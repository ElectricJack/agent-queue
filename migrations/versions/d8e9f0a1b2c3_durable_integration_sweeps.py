"""Add durable integration sweep sealing invariants.

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


revision: str = "d8e9f0a1b2c3"
down_revision: str | Sequence[str] | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH_LIFECYCLES_BEFORE = (
    "lifecycle IN ('sealing', 'sealed', 'building', 'testing', 'repairing', "
    "'human_blocked', 'promoting', 'cleanup_pending', 'promoted', 'aborted', 'failed')"
)
_BATCH_LIFECYCLES_AFTER = (
    "lifecycle IN ('sealing', 'sealed', 'building', 'testing', 'repairing', "
    "'human_blocked', 'promoting', 'cleanup_pending', 'promoted', 'aborted', 'failed', "
    "'empty')"
)
_EMPTY_IDENTITY = (
    "(lifecycle = 'empty' AND base_sha IS NULL AND integration_branch IS NULL) OR "
    "(lifecycle <> 'empty' AND base_sha IS NOT NULL AND integration_branch IS NOT NULL)"
)
_IDENTITY_COLUMNS = (
    "project_id",
    "repository_id",
    "request_id",
    "trigger",
    "source_manifest_digest",
    "base_sha",
    "integration_branch",
    "policy_snapshot",
    "artifact_snapshot",
    "created_at",
)


def _guard_legacy_batch_identity() -> None:
    invalid = op.get_bind().execute(
        sa.text(
            "SELECT id FROM integration_batches WHERE lifecycle <> 'empty' AND "
            "(base_sha IS NULL OR integration_branch IS NULL) ORDER BY id LIMIT 1"
        )
    ).first()
    if invalid is not None:
        raise RuntimeError(
            "cannot upgrade integration batch sealing schema while legacy non-empty "
            "batches have a null base SHA or integration branch; drain or reconcile "
            "those batches before upgrading"
        )


def _drop_existing_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for event in ("insert", "update", "delete"):
            op.execute(
                f"DROP TRIGGER trg_integration_members_{event} "
                "ON integration_batch_members"
            )
        op.execute(
            "DROP TRIGGER trg_integration_batch_revision_is_monotone "
            "ON integration_batches"
        )
        return
    for event in ("insert", "update", "delete"):
        op.execute(f"DROP TRIGGER trg_integration_members_{event}")
    op.execute("DROP TRIGGER trg_integration_batch_revision_monotone")


def _create_existing_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for event in ("INSERT", "UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_integration_members_{event.lower()} "
                f"BEFORE {event} ON integration_batch_members FOR EACH ROW "
                "EXECUTE FUNCTION integration_member_is_mutable()"
            )
        op.execute(
            "CREATE TRIGGER trg_integration_batch_revision_is_monotone BEFORE UPDATE "
            "ON integration_batches FOR EACH ROW EXECUTE FUNCTION "
            "integration_batch_revision_is_monotone()"
        )
        return
    op.execute(
        "CREATE TRIGGER trg_integration_members_insert BEFORE INSERT ON "
        "integration_batch_members WHEN NOT EXISTS (SELECT 1 FROM integration_batches "
        "WHERE id = NEW.batch_id AND lifecycle = 'sealing') BEGIN SELECT RAISE(ABORT, "
        "'sealed integration batch membership is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_integration_members_update BEFORE UPDATE ON "
        "integration_batch_members WHEN NOT EXISTS (SELECT 1 FROM integration_batches "
        "WHERE id = OLD.batch_id AND lifecycle = 'sealing') OR NOT EXISTS (SELECT 1 FROM "
        "integration_batches WHERE id = NEW.batch_id AND lifecycle = 'sealing') BEGIN "
        "SELECT RAISE(ABORT, 'sealed integration batch membership is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_integration_members_delete BEFORE DELETE ON "
        "integration_batch_members WHEN NOT EXISTS (SELECT 1 FROM integration_batches "
        "WHERE id = OLD.batch_id AND lifecycle = 'sealing') BEGIN SELECT RAISE(ABORT, "
        "'sealed integration batch membership is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_integration_batch_revision_monotone BEFORE UPDATE ON "
        "integration_batches WHEN NEW.current_revision < OLD.current_revision BEGIN "
        "SELECT RAISE(ABORT, 'integration batch revision cannot decrease'); END"
    )


def _backfill_review_evidence() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT m.batch_id, m.ordinal, m.task_id, m.repository_id, "
            "m.source_base_sha, m.reviewed_head_sha, m.reviewed_tree_sha, "
            "m.review_evidence, b.created_at FROM integration_batch_members m "
            "JOIN integration_batches b ON b.id = m.batch_id "
            "WHERE m.review_evidence_id IS NULL"
        ).columns(review_evidence=sa.JSON())
    ).mappings()
    evidence = sa.table(
        "integration_review_evidence",
        sa.column("id", sa.Text()),
        sa.column("source_task_id", sa.Text()),
        sa.column("repository_id", sa.Text()),
        sa.column("source_base", sa.Text()),
        sa.column("reviewed_head_sha", sa.Text()),
        sa.column("reviewed_tree_sha", sa.Text()),
        sa.column("reviewer_task_id", sa.Text()),
        sa.column("reviewer_session_attempt_id", sa.Text()),
        sa.column("review_kind", sa.Text()),
        sa.column("generation", sa.Integer()),
        sa.column("verdict", sa.Text()),
        sa.column("evidence", sa.JSON()),
        sa.column("created_at", sa.Float()),
    )
    members = sa.table(
        "integration_batch_members",
        sa.column("batch_id", sa.Text()),
        sa.column("ordinal", sa.Integer()),
        sa.column("review_evidence_id", sa.Text()),
    )
    for row in rows:
        evidence_id = f"task8a-legacy-review:{row['batch_id']}:{row['ordinal']}"
        values = {
            "id": evidence_id,
            "source_task_id": row["task_id"],
            "repository_id": row["repository_id"],
            "source_base": row["source_base_sha"],
            "reviewed_head_sha": row["reviewed_head_sha"],
            "reviewed_tree_sha": row["reviewed_tree_sha"],
            "reviewer_task_id": row["task_id"],
            "reviewer_session_attempt_id": None,
            "review_kind": "legacy_batch_member",
            "generation": 0,
            "verdict": "approved",
            "evidence": row["review_evidence"],
            "created_at": row["created_at"],
        }
        insert_fn = pg_insert if bind.dialect.name == "postgresql" else sqlite_insert
        bind.execute(
            insert_fn(evidence)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        stored = bind.execute(
            sa.select(evidence).where(evidence.c.id == evidence_id)
        ).mappings().one()
        if any(stored[field] != value for field, value in values.items()):
            raise RuntimeError(
                f"legacy review evidence identity collision for {evidence_id}"
            )
        bind.execute(
            members.update()
            .where(
                members.c.batch_id == row["batch_id"],
                members.c.ordinal == row["ordinal"],
            )
            .values(review_evidence_id=evidence_id)
        )


def _create_task8a_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        comparisons = " OR ".join(
            (
                f"NEW.{column}::text IS DISTINCT FROM OLD.{column}::text"
                if column in {"policy_snapshot", "artifact_snapshot"}
                else f"NEW.{column} IS DISTINCT FROM OLD.{column}"
            )
            for column in _IDENTITY_COLUMNS
        )
        op.execute(
            "CREATE FUNCTION integration_batch_identity_is_immutable() RETURNS trigger "
            "AS $$ BEGIN IF TG_OP = 'UPDATE' THEN IF (OLD.lifecycle <> 'sealing' OR "
            "NEW.lifecycle <> 'sealing') "
            f"AND ({comparisons}) THEN RAISE EXCEPTION 'sealed integration batch identity "
            "is immutable'; END IF; IF OLD.lifecycle <> 'sealing' AND NEW.lifecycle = "
            "'sealing' THEN RAISE EXCEPTION 'integration batch cannot return to sealing'; "
            "END IF; END IF; IF NEW.lifecycle = 'empty' AND (EXISTS (SELECT 1 FROM "
            "integration_batch_members WHERE batch_id = NEW.id) OR EXISTS (SELECT 1 FROM "
            "integration_repair_operations WHERE batch_id = NEW.id) OR EXISTS (SELECT 1 "
            "FROM project_integration_leases WHERE batch_id = NEW.id)) THEN RAISE "
            "EXCEPTION 'empty integration batch cannot retain members, repair operations, "
            "or leases'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_integration_batch_identity_immutable BEFORE INSERT OR "
            "UPDATE ON "
            "integration_batches FOR EACH ROW EXECUTE FUNCTION "
            "integration_batch_identity_is_immutable()"
        )
        op.execute(
            "CREATE FUNCTION integration_empty_batch_target_rejected() RETURNS trigger AS "
            "$$ BEGIN IF NEW.batch_id IS NOT NULL AND EXISTS (SELECT 1 FROM "
            "integration_batches WHERE id = NEW.batch_id AND lifecycle = 'empty') THEN "
            "RAISE EXCEPTION 'empty integration batch cannot have a repair operation or "
            "lease'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        for table in ("integration_repair_operations", "project_integration_leases"):
            op.execute(
                f"CREATE TRIGGER trg_{table}_reject_empty_batch BEFORE INSERT OR UPDATE "
                f"ON {table} FOR EACH ROW EXECUTE FUNCTION "
                "integration_empty_batch_target_rejected()"
            )
        return

    comparisons = " OR ".join(
        f"NEW.{column} IS NOT OLD.{column}" for column in _IDENTITY_COLUMNS
    )
    op.execute(
        "CREATE TRIGGER trg_integration_batch_identity_immutable BEFORE UPDATE ON "
        "integration_batches WHEN (OLD.lifecycle <> 'sealing' OR NEW.lifecycle <> "
        f"'sealing') AND ({comparisons}) BEGIN SELECT RAISE(ABORT, 'sealed integration "
        "batch identity is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_integration_batch_no_return_to_sealing BEFORE UPDATE ON "
        "integration_batches WHEN OLD.lifecycle <> 'sealing' AND NEW.lifecycle = "
        "'sealing' BEGIN SELECT RAISE(ABORT, 'integration batch cannot return to "
        "sealing'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_integration_batch_empty_insert_dependencies BEFORE INSERT ON "
        "integration_batches WHEN NEW.lifecycle = 'empty' AND (EXISTS (SELECT 1 FROM "
        "integration_repair_operations WHERE batch_id = NEW.id) OR EXISTS (SELECT 1 FROM "
        "project_integration_leases WHERE batch_id = NEW.id)) BEGIN SELECT RAISE(ABORT, "
        "'empty integration batch cannot retain repair operations or leases'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_integration_batch_empty_dependencies BEFORE UPDATE ON "
        "integration_batches WHEN NEW.lifecycle = 'empty' AND (EXISTS (SELECT 1 FROM "
        "integration_batch_members WHERE batch_id = NEW.id) OR EXISTS (SELECT 1 FROM "
        "integration_repair_operations WHERE batch_id = NEW.id) OR EXISTS (SELECT 1 FROM "
        "project_integration_leases WHERE batch_id = NEW.id)) BEGIN SELECT RAISE(ABORT, "
        "'empty integration batch cannot retain members, repair operations, or leases'); "
        "END"
    )
    for table in ("integration_repair_operations", "project_integration_leases"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_reject_empty_batch BEFORE INSERT ON {table} "
            "WHEN NEW.batch_id IS NOT NULL AND EXISTS (SELECT 1 FROM integration_batches "
            "WHERE id = NEW.batch_id AND lifecycle = 'empty') BEGIN SELECT RAISE(ABORT, "
            "'empty integration batch cannot have a repair operation or lease'); END"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_update_reject_empty_batch BEFORE UPDATE ON {table} "
            "WHEN NEW.batch_id IS NOT NULL AND EXISTS (SELECT 1 FROM integration_batches "
            "WHERE id = NEW.batch_id AND lifecycle = 'empty') BEGIN SELECT RAISE(ABORT, "
            "'empty integration batch cannot have a repair operation or lease'); END"
        )


def _drop_task8a_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER trg_integration_batch_identity_immutable ON integration_batches"
        )
        for table in ("integration_repair_operations", "project_integration_leases"):
            op.execute(f"DROP TRIGGER trg_{table}_reject_empty_batch ON {table}")
        op.execute("DROP FUNCTION integration_batch_identity_is_immutable()")
        op.execute("DROP FUNCTION integration_empty_batch_target_rejected()")
        return
    for trigger in (
        "trg_integration_batch_identity_immutable",
        "trg_integration_batch_no_return_to_sealing",
        "trg_integration_batch_empty_insert_dependencies",
        "trg_integration_batch_empty_dependencies",
        "trg_integration_repair_operations_reject_empty_batch",
        "trg_integration_repair_operations_update_reject_empty_batch",
        "trg_project_integration_leases_reject_empty_batch",
        "trg_project_integration_leases_update_reject_empty_batch",
    ):
        op.execute(f"DROP TRIGGER {trigger}")


def upgrade() -> None:
    _guard_legacy_batch_identity()
    _drop_existing_guards()
    with op.batch_alter_table("integration_batches") as batch:
        batch.add_column(sa.Column("request_id", sa.Text(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE integration_batches SET request_id = "
            "'task8a-legacy-request:' || id WHERE request_id IS NULL"
        )
    )
    with op.batch_alter_table("integration_batches") as batch:
        batch.alter_column("request_id", existing_type=sa.Text(), nullable=False)
        batch.drop_constraint("ck_integration_batches_lifecycle", type_="check")
        batch.create_check_constraint(
            "ck_integration_batches_lifecycle", _BATCH_LIFECYCLES_AFTER
        )
        batch.create_check_constraint(
            "ck_integration_batches_empty_identity", _EMPTY_IDENTITY
        )
        batch.create_unique_constraint(
            "uq_integration_batches_project_request", ["project_id", "request_id"]
        )

    with op.batch_alter_table("integration_batch_members") as batch:
        batch.add_column(sa.Column("review_evidence_id", sa.Text(), nullable=True))
    _backfill_review_evidence()
    with op.batch_alter_table("integration_batch_members") as batch:
        batch.alter_column(
            "review_evidence_id", existing_type=sa.Text(), nullable=False
        )
        batch.create_foreign_key(
            "fk_integration_batch_members_review_evidence",
            "integration_review_evidence",
            ["review_evidence_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    _create_existing_guards()
    _create_task8a_guards()


def downgrade() -> None:
    live_empty = op.get_bind().execute(
        sa.text("SELECT 1 FROM integration_batches WHERE lifecycle = 'empty' LIMIT 1")
    ).first()
    if live_empty is not None:
        raise RuntimeError(
            "cannot downgrade while empty integration batches exist; drain or reconcile "
            "durable sweep results first"
        )

    _drop_task8a_guards()
    _drop_existing_guards()
    with op.batch_alter_table("integration_batch_members") as batch:
        batch.drop_constraint(
            "fk_integration_batch_members_review_evidence", type_="foreignkey"
        )
        batch.drop_column("review_evidence_id")
    with op.batch_alter_table("integration_batches") as batch:
        batch.drop_constraint(
            "uq_integration_batches_project_request", type_="unique"
        )
        batch.drop_constraint("ck_integration_batches_empty_identity", type_="check")
        batch.drop_constraint("ck_integration_batches_lifecycle", type_="check")
        batch.create_check_constraint(
            "ck_integration_batches_lifecycle", _BATCH_LIFECYCLES_BEFORE
        )
        batch.drop_column("request_id")
    _create_existing_guards()
