"""cleanup irreversible prewrite fencing

Revision ID: a10c5e1e4f02
Revises: a10c5e1e4f01
Create Date: 2026-09-06 20:45:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "a10c5e1e4f02"
down_revision: str | Sequence[str] | None = "a10c5e1e4f01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "integration_cleanup_items",
        sa.Column("irreversible_nonce", sa.Text(), nullable=True),
    )
    op.add_column(
        "integration_cleanup_items",
        sa.Column("irreversible_prewrite_at", sa.Float(), nullable=True),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_check_constraint(
            "ck_integration_cleanup_items_irreversible",
            "integration_cleanup_items",
            "(irreversible_nonce IS NULL AND irreversible_prewrite_at IS NULL) OR "
            "(irreversible_nonce IS NOT NULL AND irreversible_prewrite_at IS NOT NULL)",
        )
        op.execute(
            """CREATE FUNCTION integration_cleanup_irreversible_guard() RETURNS trigger AS $$
            BEGIN
              IF OLD.irreversible_nonce IS NOT NULL AND
                 (NEW.irreversible_nonce IS DISTINCT FROM OLD.irreversible_nonce OR
                  NEW.irreversible_prewrite_at IS DISTINCT FROM OLD.irreversible_prewrite_at)
              THEN RAISE EXCEPTION 'cleanup irreversible prewrite is immutable'; END IF;
              IF NEW.irreversible_prewrite_at IS NOT NULL AND
                 (NEW.irreversible_nonce IS NULL OR
                  (OLD.irreversible_prewrite_at IS NULL AND
                   NEW.irreversible_nonce IS DISTINCT FROM OLD.execution_nonce))
              THEN RAISE EXCEPTION 'cleanup irreversible prewrite is not claim-owned'; END IF;
              RETURN NEW;
            END; $$ LANGUAGE plpgsql"""
        )
        op.execute(
            """CREATE TRIGGER trg_integration_cleanup_irreversible_guard
            BEFORE UPDATE ON integration_cleanup_items FOR EACH ROW
            EXECUTE FUNCTION integration_cleanup_irreversible_guard()"""
        )
    else:
        op.execute(
            """CREATE TRIGGER trg_integration_cleanup_irreversible_guard
            BEFORE UPDATE ON integration_cleanup_items
            WHEN
              (OLD.irreversible_nonce IS NOT NULL AND
               (NEW.irreversible_nonce IS NOT OLD.irreversible_nonce OR
                NEW.irreversible_prewrite_at IS NOT OLD.irreversible_prewrite_at))
              OR ((NEW.irreversible_nonce IS NULL) !=
                  (NEW.irreversible_prewrite_at IS NULL))
              OR (OLD.irreversible_prewrite_at IS NULL AND
                  NEW.irreversible_prewrite_at IS NOT NULL AND
                  NEW.irreversible_nonce IS NOT OLD.execution_nonce)
            BEGIN SELECT RAISE(ABORT, 'cleanup irreversible prewrite is immutable'); END"""
        )


def downgrade() -> None:
    reservation = op.get_bind().execute(
        sa.text(
            "SELECT batch_id || ':' || kind || ':' || identity "
            "FROM integration_cleanup_items "
            "WHERE irreversible_nonce IS NOT NULL "
            "OR irreversible_prewrite_at IS NOT NULL LIMIT 1"
        )
    ).scalar_one_or_none()
    if reservation is not None:
        raise RuntimeError(
            f"drain irreversible cleanup reservation {reservation} before downgrade"
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_integration_cleanup_irreversible_guard "
            "ON integration_cleanup_items"
        )
        op.execute("DROP FUNCTION IF EXISTS integration_cleanup_irreversible_guard()")
        op.drop_constraint(
            "ck_integration_cleanup_items_irreversible",
            "integration_cleanup_items",
            type_="check",
        )
    else:
        op.execute("DROP TRIGGER IF EXISTS trg_integration_cleanup_irreversible_guard")
    op.drop_column("integration_cleanup_items", "irreversible_prewrite_at")
    op.drop_column("integration_cleanup_items", "irreversible_nonce")
