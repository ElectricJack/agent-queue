"""immutable complete root delivery receipts

Revision ID: e9b2f1b7c3d5
Revises: d4a81f0c9e72
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence
import importlib
import json

import sqlalchemy as sa
from alembic import op


revision: str = "e9b2f1b7c3d5"
down_revision: str | Sequence[str] | None = "d4a81f0c9e72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_REFERENCED_INDEXES = (
    (
        "uq_integration_promotion_intents_root_identity",
        "integration_promotion_intents",
        ("id", "root_batch_id", "root_candidate_revision"),
    ),
    (
        "uq_integration_batch_members_root_identity",
        "integration_batch_members",
        (
            "batch_id",
            "ordinal",
            "task_id",
            "repository_id",
            "reviewed_head_sha",
            "reviewed_tree_sha",
            "review_evidence_id",
        ),
    ),
    (
        "uq_integration_candidate_results_root_identity",
        "integration_candidate_member_results",
        (
            "batch_id",
            "revision",
            "member_ordinal",
            "input_head_sha",
            "input_tree_sha",
            "generated_squash_sha",
        ),
    ),
    (
        "uq_integration_review_evidence_root_identity",
        "integration_review_evidence",
        (
            "id",
            "source_task_id",
            "repository_id",
            "reviewed_head_sha",
            "reviewed_tree_sha",
        ),
    ),
)


def _json_value(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def _assert_compatible() -> None:
    """Reject legacy root history whose immutable identities are not exact."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """SELECT r.*, i.intent_kind, i.root_batch_id, i.root_candidate_revision,
            m.task_id AS member_task_id, m.repository_id AS member_repository_id,
            m.reviewed_head_sha AS member_head, m.reviewed_tree_sha AS member_tree,
            m.review_evidence_id AS member_review_id,
            c.input_head_sha AS result_head, c.input_tree_sha AS result_tree,
            c.generated_squash_sha AS result_squash, c.result AS candidate_result,
            c.conflict_evidence AS candidate_evidence,
            e.source_task_id AS review_task_id, e.repository_id AS review_repository_id,
            e.reviewed_head_sha AS review_head, e.reviewed_tree_sha AS review_tree,
            e.verdict AS review_verdict
            FROM integration_root_intent_members r
            LEFT JOIN integration_promotion_intents i ON i.id = r.intent_id
            LEFT JOIN integration_batch_members m
              ON m.batch_id = r.batch_id AND m.ordinal = r.member_ordinal
            LEFT JOIN integration_candidate_member_results c
              ON c.batch_id = r.batch_id AND c.revision = r.candidate_revision
             AND c.member_ordinal = r.member_ordinal
            LEFT JOIN integration_review_evidence e ON e.id = r.review_evidence_id
            ORDER BY r.intent_id, r.member_ordinal"""
        )
    ).mappings()
    for row in rows:
        identity = f"{row['intent_id']}:{row['member_ordinal']}"
        if (
            row["intent_kind"] != "root"
            or row["root_batch_id"] != row["batch_id"]
            or row["root_candidate_revision"] != row["candidate_revision"]
        ):
            raise RuntimeError(f"root reservation {identity} has cross-intent identity")
        if (
            row["member_task_id"] != row["source_task_id"]
            or row["member_repository_id"] != row["repository_id"]
            or row["member_head"] != row["reviewed_head_sha"]
            or row["member_tree"] != row["reviewed_tree_sha"]
            or row["member_review_id"] != row["review_evidence_id"]
        ):
            raise RuntimeError(f"root reservation {identity} mismatches sealed member")
        if (
            row["candidate_result"] != "applied"
            or row["result_head"] != row["reviewed_head_sha"]
            or row["result_tree"] != row["reviewed_tree_sha"]
            or row["result_squash"] != row["generated_squash_sha"]
        ):
            raise RuntimeError(f"root reservation {identity} mismatches candidate result")
        if (
            row["review_task_id"] != row["source_task_id"]
            or row["review_repository_id"] != row["repository_id"]
            or row["review_head"] != row["reviewed_head_sha"]
            or row["review_tree"] != row["reviewed_tree_sha"]
            or row["review_verdict"] != "approved"
        ):
            raise RuntimeError(f"root reservation {identity} mismatches review subject")
        if _json_value(row["result_evidence"]) != (
            _json_value(row["candidate_evidence"]) or {}
        ):
            raise RuntimeError(f"root reservation {identity} has result-evidence drift")

    duplicate = bind.execute(
        sa.text(
            """SELECT batch_id, candidate_revision, member_ordinal
            FROM task_delivery_receipts WHERE batch_id IS NOT NULL
            GROUP BY batch_id, candidate_revision, member_ordinal HAVING COUNT(*) > 1
            ORDER BY batch_id, candidate_revision, member_ordinal LIMIT 1"""
        )
    ).mappings().one_or_none()
    if duplicate is not None:
        raise RuntimeError(
            "duplicate root receipt tuple "
            f"{duplicate['batch_id']}:{duplicate['candidate_revision']}:"
            f"{duplicate['member_ordinal']}"
        )

    receipts = bind.execute(
        sa.text(
            """SELECT d.id, d.batch_id, d.candidate_revision, d.member_ordinal,
            r.receipt_id, r.intent_id
            FROM task_delivery_receipts d
            LEFT JOIN integration_root_intent_members r ON r.receipt_id = d.id
            WHERE d.batch_id IS NOT NULL ORDER BY d.id"""
        )
    ).mappings()
    for row in receipts:
        if (
            row["receipt_id"] is None
            or row["batch_id"] is None
            or row["candidate_revision"] is None
            or row["member_ordinal"] is None
        ):
            raise RuntimeError(f"root receipt {row['id']} has no exact reservation")
        reserved = bind.execute(
            sa.text(
                """SELECT batch_id, candidate_revision, member_ordinal
                FROM integration_root_intent_members WHERE receipt_id = :receipt_id"""
            ),
            {"receipt_id": row["id"]},
        ).mappings().one()
        if (
            reserved["batch_id"] != row["batch_id"]
            or reserved["candidate_revision"] != row["candidate_revision"]
            or reserved["member_ordinal"] != row["member_ordinal"]
        ):
            raise RuntimeError(f"root receipt {row['id']} has cross-intent identity")


def _drop_receipt_guards() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for event in ("update", "delete"):
            op.execute(
                f"DROP TRIGGER IF EXISTS trg_task_delivery_receipts_{event} "
                "ON task_delivery_receipts"
            )
    else:
        for event in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_task_delivery_receipts_{event}")


def _create_receipt_guards() -> None:
    _drop_receipt_guards()
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """CREATE OR REPLACE FUNCTION task_delivery_receipt_append_only()
            RETURNS trigger AS $$ BEGIN
            RAISE EXCEPTION 'task delivery receipts are append-only'; END;
            $$ LANGUAGE plpgsql"""
        )
        for event in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_task_delivery_receipts_{event.lower()} BEFORE {event} ON "
                "task_delivery_receipts FOR EACH ROW EXECUTE FUNCTION "
                "task_delivery_receipt_append_only()"
            )
        return
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_task_delivery_receipts_{event.lower()} BEFORE {event} ON "
            "task_delivery_receipts BEGIN SELECT RAISE(ABORT, "
            "'task delivery receipts are append-only'); END"
        )


def _root_guards():
    return importlib.import_module(
        "migrations.versions.d4a81f0c9e72_root_main_promotion_intents"
    )


def _create_root_guards() -> None:
    guards = _root_guards()
    guards._create_root_guards()
    if op.get_bind().dialect.name == "postgresql":
        # PostgreSQL ``json`` has no equality operator, so a row-wide
        # ``NEW IS DISTINCT FROM OLD`` cannot guard this JSON-bearing table.
        # Match SQLite's stricter terminal contract: every later UPDATE fails.
        op.execute(
            """CREATE OR REPLACE FUNCTION integration_root_intent_terminal_guard()
            RETURNS trigger AS $$ BEGIN
            IF OLD.intent_kind = 'root' AND OLD.state IN ('committed', 'superseded')
            THEN RAISE EXCEPTION 'terminal root promotion intent is immutable'; END IF;
            RETURN NEW; END; $$ LANGUAGE plpgsql"""
        )


def _create_postgres_prepared_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    previous = importlib.import_module(
        "migrations.versions.b91e4d7a2c10_prepared_promotion_evidence"
    )
    root = _root_guards()
    columns = previous._NEW_IDENTITY + root._ROOT_INTENT_COLUMNS
    json_columns = {"review_evidence", "authors", "provenance", "commit_metadata"}
    comparisons = " OR ".join(
        (
            f"NEW.{name}::text IS DISTINCT FROM OLD.{name}::text"
            if name in json_columns
            else f"NEW.{name} IS DISTINCT FROM OLD.{name}"
        )
        for name in columns
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION integration_prepared_identity_immutable() "
        "RETURNS trigger AS $$ BEGIN "
        f"IF OLD.prepared_sha IS NOT NULL AND ({comparisons}) THEN "
        "RAISE EXCEPTION 'prepared integration identity is immutable'; "
        "END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )


def _replace_root_member_foreign_keys(*, exact: bool) -> None:
    with op.batch_alter_table("integration_root_intent_members") as batch:
        if exact:
            batch.drop_constraint("fk_integration_root_intent_members_intent", type_="foreignkey")
            batch.drop_constraint("fk_integration_root_intent_members_result", type_="foreignkey")
            batch.create_foreign_key(
                "fk_integration_root_intent_members_exact_intent",
                "integration_promotion_intents",
                ["intent_id", "batch_id", "candidate_revision"],
                ["id", "root_batch_id", "root_candidate_revision"],
                ondelete="RESTRICT",
            )
            batch.create_foreign_key(
                "fk_integration_root_intent_members_exact_member",
                "integration_batch_members",
                [
                    "batch_id", "member_ordinal", "source_task_id", "repository_id",
                    "reviewed_head_sha", "reviewed_tree_sha", "review_evidence_id",
                ],
                [
                    "batch_id", "ordinal", "task_id", "repository_id",
                    "reviewed_head_sha", "reviewed_tree_sha", "review_evidence_id",
                ],
                ondelete="RESTRICT",
            )
            batch.create_foreign_key(
                "fk_integration_root_intent_members_exact_result",
                "integration_candidate_member_results",
                [
                    "batch_id", "candidate_revision", "member_ordinal",
                    "reviewed_head_sha", "reviewed_tree_sha", "generated_squash_sha",
                ],
                [
                    "batch_id", "revision", "member_ordinal", "input_head_sha",
                    "input_tree_sha", "generated_squash_sha",
                ],
                ondelete="RESTRICT",
            )
            batch.create_foreign_key(
                "fk_integration_root_intent_members_exact_review",
                "integration_review_evidence",
                [
                    "review_evidence_id", "source_task_id", "repository_id",
                    "reviewed_head_sha", "reviewed_tree_sha",
                ],
                [
                    "id", "source_task_id", "repository_id", "reviewed_head_sha",
                    "reviewed_tree_sha",
                ],
                ondelete="RESTRICT",
            )
            return
        for name in (
            "fk_integration_root_intent_members_exact_review",
            "fk_integration_root_intent_members_exact_result",
            "fk_integration_root_intent_members_exact_member",
            "fk_integration_root_intent_members_exact_intent",
        ):
            batch.drop_constraint(name, type_="foreignkey")
        batch.create_foreign_key(
            "fk_integration_root_intent_members_intent",
            "integration_promotion_intents",
            ["intent_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_integration_root_intent_members_result",
            "integration_candidate_member_results",
            ["batch_id", "candidate_revision", "member_ordinal"],
            ["batch_id", "revision", "member_ordinal"],
            ondelete="RESTRICT",
        )


def upgrade() -> None:
    _assert_compatible()
    guards = _root_guards()
    guards._drop_root_guards()
    _drop_receipt_guards()
    for name, table, columns in _REFERENCED_INDEXES:
        op.create_index(name, table, list(columns), unique=True)
    _replace_root_member_foreign_keys(exact=True)
    op.create_index(
        "uq_task_delivery_receipts_root_tuple",
        "task_delivery_receipts",
        ["batch_id", "candidate_revision", "member_ordinal"],
        unique=True,
        sqlite_where=sa.text("batch_id IS NOT NULL"),
        postgresql_where=sa.text("batch_id IS NOT NULL"),
    )
    _create_receipt_guards()
    _create_root_guards()
    _create_postgres_prepared_guard()


def downgrade() -> None:
    bind = op.get_bind()
    live = bind.execute(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM integration_promotion_intents "
            "WHERE intent_kind = 'root') + "
            "(SELECT COUNT(*) FROM integration_root_intent_members) + "
            "(SELECT COUNT(*) FROM task_delivery_receipts WHERE batch_id IS NOT NULL) + "
            "(SELECT COUNT(*) FROM integration_candidate_ref_mutations "
            "WHERE purpose = 'root_main')"
        )
    ).scalar_one()
    if live:
        raise RuntimeError("drain or reconcile root promotion history before downgrade")

    guards = _root_guards()
    guards._drop_root_guards()
    _drop_receipt_guards()
    op.drop_index(
        "uq_task_delivery_receipts_root_tuple", table_name="task_delivery_receipts"
    )
    _replace_root_member_foreign_keys(exact=False)
    for name, table, _columns in reversed(_REFERENCED_INDEXES):
        op.drop_index(name, table_name=table)
    _create_receipt_guards()
    _create_root_guards()
    _create_postgres_prepared_guard()
