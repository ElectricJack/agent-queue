"""durable exclusive attestation publication claims

Revision ID: f0a1b2c3d4e5
Revises: ed46f4aec7be
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "f0a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "ed46f4aec7be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_IDENTITY = (
    "id", "project_id", "batch_id", "revision", "operation_id", "head_sha",
    "ci_evidence_id", "external_id", "created_at",
)


def _create_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """CREATE FUNCTION integration_attestation_publication_guard()
            RETURNS trigger AS $$ BEGIN
            IF TG_OP = 'DELETE' THEN
              RAISE EXCEPTION 'attestation reservations are durable'; END IF;
            IF OLD.state = 'published' AND NEW IS DISTINCT FROM OLD THEN
              RAISE EXCEPTION 'published attestation reservation is immutable'; END IF;
            IF OLD.prewrite_at IS NOT NULL AND
              NEW.prewrite_at IS DISTINCT FROM OLD.prewrite_at THEN
              RAISE EXCEPTION 'attestation prewrite marker is immutable'; END IF;
            IF OLD.prewrite_at IS NOT NULL AND
              NEW.execution_nonce IS DISTINCT FROM OLD.execution_nonce THEN
              RAISE EXCEPTION 'marked attestation execution nonce is immutable'; END IF;
            IF """ + " OR ".join(
                f"NEW.{name} IS DISTINCT FROM OLD.{name}" for name in _IDENTITY
            ) + """ THEN
              RAISE EXCEPTION 'attestation reservation identity is immutable'; END IF;
            RETURN NEW; END; $$ LANGUAGE plpgsql"""
        )
        op.execute(
            "CREATE TRIGGER trg_integration_attestation_publication_guard BEFORE UPDATE OR DELETE ON "
            "integration_attestation_publications FOR EACH ROW EXECUTE FUNCTION "
            "integration_attestation_publication_guard()"
        )
        return
    op.execute(
        "CREATE TRIGGER trg_integration_attestation_publication_guard BEFORE UPDATE ON "
        "integration_attestation_publications WHEN OLD.state = 'published' OR "
        "(OLD.prewrite_at IS NOT NULL AND NEW.prewrite_at IS NOT OLD.prewrite_at) OR "
        "(OLD.prewrite_at IS NOT NULL AND NEW.execution_nonce IS NOT OLD.execution_nonce) OR "
        + " OR ".join(f"NEW.{name} IS NOT OLD.{name}" for name in _IDENTITY)
        + " BEGIN SELECT RAISE(ABORT, 'attestation reservation is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_integration_attestation_publication_delete BEFORE DELETE ON "
        "integration_attestation_publications BEGIN SELECT RAISE(ABORT, "
        "'attestation reservations are durable'); END"
    )


def upgrade() -> None:
    op.create_table(
        "integration_attestation_publications",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("head_sha", sa.Text(), nullable=False),
        sa.Column("ci_evidence_id", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("execution_nonce", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("prewrite_at", sa.Float(), nullable=True),
        sa.Column("check_run_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.UniqueConstraint(
            "batch_id", "revision", name="uq_integration_attestation_publications_subject"
        ),
        sa.UniqueConstraint(
            "external_id", name="uq_integration_attestation_publications_external"
        ),
        sa.CheckConstraint(
            "revision >= 0", name="ck_integration_attestation_publications_revision"
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'published')",
            name="ck_integration_attestation_publications_state",
        ),
        sa.CheckConstraint(
            "(state = 'reserved' AND check_run_id IS NULL) OR "
            "(state = 'published' AND prewrite_at IS NOT NULL AND check_run_id > 0)",
            name="ck_integration_attestation_publications_result",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id", "revision"],
            ["integration_candidate_revisions.batch_id", "integration_candidate_revisions.revision"],
            name="fk_integration_attestation_publications_revision", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            name="fk_integration_attestation_publications_project", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["integration_repair_operations.id"],
            name="fk_integration_attestation_publications_operation", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ci_evidence_id"], ["integration_check_evidence.id"],
            name="fk_integration_attestation_publications_evidence", ondelete="RESTRICT",
        ),
    )
    _create_guards()


def downgrade() -> None:
    row = op.get_bind().execute(
        sa.text("SELECT id FROM integration_attestation_publications ORDER BY id LIMIT 1")
    ).scalar_one_or_none()
    if row is not None:
        raise RuntimeError("drain attestation publication reservations before downgrade")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_integration_attestation_publication_guard ON "
            "integration_attestation_publications"
        )
        op.execute("DROP FUNCTION IF EXISTS integration_attestation_publication_guard()")
    else:
        op.execute("DROP TRIGGER IF EXISTS trg_integration_attestation_publication_guard")
        op.execute("DROP TRIGGER IF EXISTS trg_integration_attestation_publication_delete")
    op.drop_table("integration_attestation_publications")
