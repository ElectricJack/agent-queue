"""Add reviewed evidence and frozen prepared-promotion inputs.

Revision ID: b91e4d7a2c10
Revises: f02a4a4a3010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b91e4d7a2c10"
down_revision: str | Sequence[str] | None = "f02a4a4a3010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_IDENTITY = (
    "domain_key",
    "receipt_id",
    "source_task_id",
    "source_head",
    "source_base",
    "repository_id",
    "target_branch",
    "expected_target",
    "prepared_sha",
    "fence_owner_id",
    "fence_token",
    "recovery_ref",
)
_NEW_IDENTITY = _OLD_IDENTITY + (
    "operation_key",
    "project_id",
    "target_task_id",
    "origin_url",
    "review_evidence",
    "authors",
    "provenance",
    "commit_metadata",
)


def _drop_prepared_guard() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER trg_integration_prepared_identity_immutable "
            "ON integration_promotion_intents"
        )
    else:
        op.execute("DROP TRIGGER trg_integration_prepared_identity_immutable")


def _create_prepared_guard(columns: tuple[str, ...]) -> None:
    if op.get_bind().dialect.name == "postgresql":
        comparisons = " OR ".join(f"NEW.{name} IS DISTINCT FROM OLD.{name}" for name in columns)
        op.execute(
            "CREATE OR REPLACE FUNCTION integration_prepared_identity_immutable() "
            "RETURNS trigger AS $$ BEGIN "
            f"IF OLD.prepared_sha IS NOT NULL AND ({comparisons}) THEN "
            "RAISE EXCEPTION 'prepared integration identity is immutable'; "
            "END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_integration_prepared_identity_immutable BEFORE UPDATE ON "
            "integration_promotion_intents FOR EACH ROW EXECUTE FUNCTION "
            "integration_prepared_identity_immutable()"
        )
        return
    comparisons = " OR ".join(f"NEW.{name} IS NOT OLD.{name}" for name in columns)
    op.execute(
        "CREATE TRIGGER trg_integration_prepared_identity_immutable BEFORE UPDATE ON "
        "integration_promotion_intents WHEN OLD.prepared_sha IS NOT NULL AND "
        f"({comparisons}) BEGIN SELECT RAISE(ABORT, "
        "'prepared integration identity is immutable'); END"
    )


def _create_review_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION integration_review_evidence_append_only() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'integration review evidence is append-only'; END; "
            "$$ LANGUAGE plpgsql"
        )
        for event in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_integration_review_evidence_{event.lower()} "
                f"BEFORE {event} ON integration_review_evidence FOR EACH ROW "
                "EXECUTE FUNCTION integration_review_evidence_append_only()"
            )
        return
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_integration_review_evidence_{event.lower()} "
            f"BEFORE {event} ON integration_review_evidence BEGIN SELECT RAISE(ABORT, "
            "'integration review evidence is append-only'); END"
        )


def _drop_review_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for event in ("update", "delete"):
            op.execute(
                f"DROP TRIGGER trg_integration_review_evidence_{event} "
                "ON integration_review_evidence"
            )
        op.execute("DROP FUNCTION integration_review_evidence_append_only()")
        return
    for event in ("update", "delete"):
        op.execute(f"DROP TRIGGER trg_integration_review_evidence_{event}")


def upgrade() -> None:
    _drop_prepared_guard()
    with op.batch_alter_table("integration_promotion_intents") as batch:
        batch.add_column(sa.Column("operation_key", sa.Text(), nullable=True))
        batch.add_column(sa.Column("project_id", sa.Text(), nullable=True))
        batch.add_column(sa.Column("target_task_id", sa.Text(), nullable=True))
        batch.add_column(sa.Column("origin_url", sa.Text(), nullable=True))
        batch.add_column(sa.Column("review_evidence", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("authors", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("provenance", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("commit_metadata", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("conflict_diagnostics", sa.JSON(), nullable=True))
    op.create_index(
        "uq_integration_promotion_intents_unresolved_target",
        "integration_promotion_intents",
        ["repository_id", "target_branch"],
        unique=True,
        sqlite_where=sa.text("state NOT IN ('committed', 'conflict')"),
        postgresql_where=sa.text("state NOT IN ('committed', 'conflict')"),
    )
    _create_prepared_guard(_NEW_IDENTITY)

    op.create_table(
        "integration_review_evidence",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("source_task_id", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("source_base", sa.Text(), nullable=False),
        sa.Column("reviewed_head_sha", sa.Text(), nullable=False),
        sa.Column("reviewed_tree_sha", sa.Text(), nullable=False),
        sa.Column("reviewer_task_id", sa.Text(), nullable=False),
        sa.Column("reviewer_session_attempt_id", sa.Text(), nullable=True),
        sa.Column("review_kind", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint("generation >= 0", name="ck_integration_review_evidence_generation"),
        sa.CheckConstraint(
            "verdict IN ('approved', 'rejected')",
            name="ck_integration_review_evidence_verdict",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_integration_review_evidence_current",
        "integration_review_evidence",
        ["source_task_id", "repository_id", "generation", "created_at"],
    )
    _create_review_guards()


def downgrade() -> None:
    _drop_review_guards()
    op.drop_index(
        "idx_integration_review_evidence_current",
        table_name="integration_review_evidence",
    )
    op.drop_table("integration_review_evidence")

    _drop_prepared_guard()
    op.drop_index(
        "uq_integration_promotion_intents_unresolved_target",
        table_name="integration_promotion_intents",
    )
    with op.batch_alter_table("integration_promotion_intents") as batch:
        for column in (
            "conflict_diagnostics",
            "commit_metadata",
            "provenance",
            "authors",
            "review_evidence",
            "origin_url",
            "target_task_id",
            "project_id",
            "operation_key",
        ):
            batch.drop_column(column)
    _create_prepared_guard(_OLD_IDENTITY)
