"""root main promotion intents

Revision ID: d4a81f0c9e72
Revises: 46f910d0dce6
Create Date: 2026-09-05
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from migrations.sqlite_triggers import preserve_sqlite_triggers

revision: str = "d4a81f0c9e72"
down_revision: str | Sequence[str] | None = "46f910d0dce6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ROOT_INTENT_COLUMNS = (
    "intent_kind",
    "root_batch_id",
    "root_candidate_revision",
    "project_lease_owner_id",
    "project_lease_fence_token",
    "branch_fence_owner_id",
    "branch_fence_token",
    "ci_evidence_id",
)


def _drop_prepared_guard() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_integration_prepared_identity_immutable "
            "ON integration_promotion_intents"
        )
    else:
        op.execute("DROP TRIGGER IF EXISTS trg_integration_prepared_identity_immutable")


def _create_prepared_guard() -> None:
    previous = importlib.import_module(
        "migrations.versions.b91e4d7a2c10_prepared_promotion_evidence"
    )
    previous._create_prepared_guard(previous._NEW_IDENTITY + _ROOT_INTENT_COLUMNS)


def _drop_root_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for trigger, table in (
            ("trg_integration_root_intent_terminal", "integration_promotion_intents"),
            ("trg_integration_root_member_update", "integration_root_intent_members"),
            ("trg_integration_root_member_delete", "integration_root_intent_members"),
            ("trg_integration_root_prewrite_immutable", "integration_candidate_ref_mutations"),
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        for function in (
            "integration_root_intent_terminal_guard()",
            "integration_root_member_append_only()",
            "integration_root_prewrite_immutable()",
        ):
            op.execute(f"DROP FUNCTION IF EXISTS {function}")
        return
    for trigger in (
        "trg_integration_root_intent_terminal",
        "trg_integration_root_member_update",
        "trg_integration_root_member_delete",
        "trg_integration_root_prewrite_immutable",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _create_root_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """CREATE FUNCTION integration_root_intent_terminal_guard()
            RETURNS trigger AS $$ BEGIN
            IF OLD.intent_kind = 'root' AND OLD.state IN ('committed', 'superseded') AND
              NEW IS DISTINCT FROM OLD
            THEN RAISE EXCEPTION 'terminal root promotion intent is immutable'; END IF;
            RETURN NEW; END; $$ LANGUAGE plpgsql"""
        )
        op.execute(
            "CREATE TRIGGER trg_integration_root_intent_terminal BEFORE UPDATE ON "
            "integration_promotion_intents FOR EACH ROW EXECUTE FUNCTION "
            "integration_root_intent_terminal_guard()"
        )
        op.execute(
            "CREATE FUNCTION integration_root_member_append_only() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'root intent member reservations are append-only'; END; "
            "$$ LANGUAGE plpgsql"
        )
        for event in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_integration_root_member_{event.lower()} BEFORE {event} ON "
                "integration_root_intent_members FOR EACH ROW EXECUTE FUNCTION "
                "integration_root_member_append_only()"
            )
        op.execute(
            """CREATE FUNCTION integration_root_prewrite_immutable()
            RETURNS trigger AS $$ BEGIN
            IF OLD.purpose = 'root_main' AND OLD.state = 'superseded' AND
              NEW IS DISTINCT FROM OLD
            THEN RAISE EXCEPTION 'superseded root main claim is immutable'; END IF;
            IF OLD.prewrite_at IS NOT NULL AND NEW.prewrite_at IS DISTINCT FROM OLD.prewrite_at
            THEN RAISE EXCEPTION 'root main prewrite marker is immutable'; END IF;
            RETURN NEW; END; $$ LANGUAGE plpgsql"""
        )
        op.execute(
            "CREATE TRIGGER trg_integration_root_prewrite_immutable BEFORE UPDATE ON "
            "integration_candidate_ref_mutations FOR EACH ROW EXECUTE FUNCTION "
            "integration_root_prewrite_immutable()"
        )
        return
    op.execute(
        "CREATE TRIGGER trg_integration_root_intent_terminal BEFORE UPDATE ON "
        "integration_promotion_intents WHEN OLD.intent_kind = 'root' AND "
        "OLD.state IN ('committed', 'superseded') BEGIN SELECT RAISE(ABORT, "
        "'terminal root promotion intent is immutable'); END"
    )
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_integration_root_member_{event.lower()} BEFORE {event} ON "
            "integration_root_intent_members BEGIN SELECT RAISE(ABORT, "
            "'root intent member reservations are append-only'); END"
        )
    op.execute(
        "CREATE TRIGGER trg_integration_root_prewrite_immutable BEFORE UPDATE ON "
        "integration_candidate_ref_mutations WHEN "
        "(OLD.purpose = 'root_main' AND OLD.state = 'superseded') OR "
        "(OLD.prewrite_at IS NOT NULL AND NEW.prewrite_at IS NOT OLD.prewrite_at) "
        "BEGIN SELECT RAISE(ABORT, 'terminal root main claim is immutable'); END"
    )


def _replace_mutation_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_candidate_mutation_identity")
    op.execute("DROP TRIGGER IF EXISTS trg_candidate_mutation_applied")
    previous = importlib.import_module(
        "migrations.versions.e1eab6dbc186_candidate_durable_mutation_claims"
    )
    previous._create_mutation_guards()


def upgrade() -> None:
    _drop_prepared_guard()
    op.drop_index(
        "uq_integration_promotion_intents_unresolved_target",
        table_name="integration_promotion_intents",
    )
    with op.batch_alter_table("integration_promotion_intents") as batch:
        batch.drop_constraint("ck_integration_promotion_intents_state", type_="check")
        batch.add_column(
            sa.Column("intent_kind", sa.Text(), nullable=False, server_default="child")
        )
        for name, type_ in (
            ("root_batch_id", sa.Text()),
            ("root_candidate_revision", sa.Integer()),
            ("project_lease_owner_id", sa.Text()),
            ("project_lease_fence_token", sa.Integer()),
            ("branch_fence_owner_id", sa.Text()),
            ("branch_fence_token", sa.Integer()),
            ("ci_evidence_id", sa.Text()),
        ):
            batch.add_column(sa.Column(name, type_, nullable=True))
        batch.create_check_constraint(
            "ck_integration_promotion_intents_state",
            "state IN ('reserved', 'prepared', 'pushed', 'reconciled', 'committed', "
            "'conflict', 'resolution_reserved', 'superseded')",
        )
        batch.create_check_constraint(
            "ck_integration_promotion_intents_kind_binding",
            "(intent_kind = 'child' AND root_batch_id IS NULL AND "
            "root_candidate_revision IS NULL AND project_lease_owner_id IS NULL AND "
            "project_lease_fence_token IS NULL AND branch_fence_owner_id IS NULL AND "
            "branch_fence_token IS NULL AND ci_evidence_id IS NULL) OR "
            "(intent_kind = 'root' AND root_batch_id IS NOT NULL AND "
            "root_candidate_revision IS NOT NULL AND root_candidate_revision >= 0 AND "
            "project_lease_owner_id IS NOT NULL AND project_lease_fence_token IS NOT NULL AND "
            "project_lease_fence_token >= 0 AND branch_fence_owner_id IS NOT NULL AND "
            "branch_fence_token IS NOT NULL AND branch_fence_token >= 0 AND "
            "ci_evidence_id IS NOT NULL AND source_task_id IS NULL AND target_task_id IS NULL)",
        )
        batch.create_foreign_key(
            "fk_integration_promotion_intents_root_revision",
            "integration_candidate_revisions",
            ["root_batch_id", "root_candidate_revision"],
            ["batch_id", "revision"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "uq_integration_promotion_intents_unresolved_target",
        "integration_promotion_intents",
        ["repository_id", "target_branch"],
        unique=True,
        sqlite_where=sa.text("state NOT IN ('committed', 'conflict', 'superseded')"),
        postgresql_where=sa.text("state NOT IN ('committed', 'conflict', 'superseded')"),
    )
    _create_prepared_guard()

    with (
        preserve_sqlite_triggers("task_delivery_receipts"),
        op.batch_alter_table("task_delivery_receipts") as batch,
    ):
        batch.create_check_constraint(
            "ck_task_delivery_receipts_root_tuple",
            "(batch_id IS NULL AND member_ordinal IS NULL AND candidate_revision IS NULL) OR "
            "(batch_id IS NOT NULL AND member_ordinal IS NOT NULL AND "
            "candidate_revision IS NOT NULL)",
        )
        batch.create_foreign_key(
            "fk_task_delivery_receipts_root_member",
            "integration_batch_members",
            ["batch_id", "member_ordinal"],
            ["batch_id", "ordinal"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_task_delivery_receipts_root_result",
            "integration_candidate_member_results",
            ["batch_id", "candidate_revision", "member_ordinal"],
            ["batch_id", "revision", "member_ordinal"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("integration_candidate_ref_mutations") as batch:
        batch.drop_constraint("ck_integration_candidate_ref_mutations_purpose", type_="check")
        batch.drop_constraint("ck_integration_candidate_ref_mutations_state", type_="check")
        batch.drop_constraint("ck_integration_candidate_ref_mutations_remote", type_="check")
        batch.add_column(sa.Column("prewrite_at", sa.Float(), nullable=True))
        batch.create_check_constraint(
            "ck_integration_candidate_ref_mutations_purpose",
            "purpose IN ('candidate_final', 'candidate_partial', 'repair_resolution', "
            "'repair_handoff', 'root_main')",
        )
        batch.create_check_constraint(
            "ck_integration_candidate_ref_mutations_state",
            "state IN ('reserved', 'applied', 'superseded')",
        )
        batch.create_check_constraint(
            "ck_integration_candidate_ref_mutations_remote",
            "(state = 'reserved' AND remote_sha IS NULL) OR "
            "(state = 'applied' AND remote_sha = desired_sha) OR "
            "(state = 'superseded' AND purpose = 'root_main' AND remote_sha IS NULL)",
        )
    _replace_mutation_guard()

    op.create_table(
        "integration_root_intent_members",
        sa.Column("intent_id", sa.Text(), nullable=False),
        sa.Column("member_ordinal", sa.Integer(), nullable=False),
        sa.Column("receipt_id", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("candidate_revision", sa.Integer(), nullable=False),
        sa.Column("source_task_id", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("reviewed_head_sha", sa.Text(), nullable=False),
        sa.Column("reviewed_tree_sha", sa.Text(), nullable=False),
        sa.Column("generated_squash_sha", sa.Text(), nullable=False),
        sa.Column("result_evidence", sa.JSON(), nullable=False),
        sa.Column("review_evidence_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "member_ordinal >= 0", name="ck_integration_root_intent_members_ordinal"
        ),
        sa.CheckConstraint(
            "candidate_revision >= 0", name="ck_integration_root_intent_members_revision"
        ),
        sa.ForeignKeyConstraint(
            ["intent_id"], ["integration_promotion_intents.id"],
            name="fk_integration_root_intent_members_intent", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["batch_id", "candidate_revision", "member_ordinal"],
            ["integration_candidate_member_results.batch_id",
             "integration_candidate_member_results.revision",
             "integration_candidate_member_results.member_ordinal"],
            name="fk_integration_root_intent_members_result", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("intent_id", "member_ordinal"),
        sa.UniqueConstraint("receipt_id", name="uq_integration_root_intent_members_receipt"),
    )
    _create_root_guards()


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
        raise RuntimeError(
            "drain or reconcile root promotion history before downgrade"
        )
    _drop_root_guards()
    op.drop_table("integration_root_intent_members")

    if bind.dialect.name != "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_candidate_mutation_identity")
        op.execute("DROP TRIGGER IF EXISTS trg_candidate_mutation_applied")
    with op.batch_alter_table("integration_candidate_ref_mutations") as batch:
        batch.drop_constraint("ck_integration_candidate_ref_mutations_purpose", type_="check")
        batch.drop_constraint("ck_integration_candidate_ref_mutations_state", type_="check")
        batch.drop_constraint("ck_integration_candidate_ref_mutations_remote", type_="check")
        batch.drop_column("prewrite_at")
        batch.create_check_constraint(
            "ck_integration_candidate_ref_mutations_purpose",
            "purpose IN ('candidate_final', 'candidate_partial', 'repair_resolution', "
            "'repair_handoff')",
        )
        batch.create_check_constraint(
            "ck_integration_candidate_ref_mutations_state",
            "state IN ('reserved', 'applied')",
        )
        batch.create_check_constraint(
            "ck_integration_candidate_ref_mutations_remote",
            "(state = 'reserved' AND remote_sha IS NULL) OR "
            "(state = 'applied' AND remote_sha = desired_sha)",
        )
    _replace_mutation_guard()

    with (
        preserve_sqlite_triggers("task_delivery_receipts"),
        op.batch_alter_table("task_delivery_receipts") as batch,
    ):
        batch.drop_constraint("fk_task_delivery_receipts_root_result", type_="foreignkey")
        batch.drop_constraint("fk_task_delivery_receipts_root_member", type_="foreignkey")
        batch.drop_constraint("ck_task_delivery_receipts_root_tuple", type_="check")

    _drop_prepared_guard()
    op.drop_index(
        "uq_integration_promotion_intents_unresolved_target",
        table_name="integration_promotion_intents",
    )
    with op.batch_alter_table("integration_promotion_intents") as batch:
        batch.drop_constraint(
            "fk_integration_promotion_intents_root_revision", type_="foreignkey"
        )
        batch.drop_constraint("ck_integration_promotion_intents_kind_binding", type_="check")
        batch.drop_constraint("ck_integration_promotion_intents_state", type_="check")
        for column in reversed(_ROOT_INTENT_COLUMNS):
            batch.drop_column(column)
        batch.create_check_constraint(
            "ck_integration_promotion_intents_state",
            "state IN ('reserved', 'prepared', 'pushed', 'reconciled', 'committed', "
            "'conflict', 'resolution_reserved')",
        )
    op.create_index(
        "uq_integration_promotion_intents_unresolved_target",
        "integration_promotion_intents",
        ["repository_id", "target_branch"],
        unique=True,
        sqlite_where=sa.text("state NOT IN ('committed', 'conflict')"),
        postgresql_where=sa.text("state NOT IN ('committed', 'conflict')"),
    )
    previous = importlib.import_module(
        "migrations.versions.b91e4d7a2c10_prepared_promotion_evidence"
    )
    previous._create_prepared_guard(previous._NEW_IDENTITY)
