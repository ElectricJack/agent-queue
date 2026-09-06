"""immutable integration release results

Revision ID: a10c5e1e4f01
Revises: 18cd4540cd0d
Create Date: 2026-09-06 20:15:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "a10c5e1e4f01"
down_revision: str | Sequence[str] | None = "18cd4540cd0d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "integration_release_results",
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("catchup_request_id", sa.Text(), nullable=True),
        sa.Column("released_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["integration_batches.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("batch_id"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """CREATE FUNCTION integration_release_result_immutable() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'integration release result is immutable'; END;
            $$ LANGUAGE plpgsql"""
        )
        op.execute(
            """CREATE TRIGGER trg_integration_release_result_immutable
            BEFORE UPDATE OR DELETE ON integration_release_results
            FOR EACH ROW EXECUTE FUNCTION integration_release_result_immutable()"""
        )
    else:
        op.execute(
            """CREATE TRIGGER trg_integration_release_result_update
            BEFORE UPDATE ON integration_release_results
            BEGIN SELECT RAISE(ABORT, 'integration release result is immutable'); END"""
        )
        op.execute(
            """CREATE TRIGGER trg_integration_release_result_delete
            BEFORE DELETE ON integration_release_results
            BEGIN SELECT RAISE(ABORT, 'integration release result is immutable'); END"""
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_integration_release_result_immutable "
            "ON integration_release_results"
        )
        op.execute("DROP FUNCTION IF EXISTS integration_release_result_immutable()")
    else:
        op.execute("DROP TRIGGER IF EXISTS trg_integration_release_result_update")
        op.execute("DROP TRIGGER IF EXISTS trg_integration_release_result_delete")
    op.drop_table("integration_release_results")
