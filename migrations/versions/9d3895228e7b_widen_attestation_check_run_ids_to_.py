"""Widen attestation check-run IDs to bigint.

Revision ID: 9d3895228e7b
Revises: a12a5e1e4f05
Create Date: 2026-09-07 14:15:56.909391

GitHub check-run IDs exceed the signed 32-bit range. SQLite INTEGER already
stores signed 64-bit values; leave its table and publication guards intact.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "9d3895228e7b"
down_revision: str | Sequence[str] | None = "a12a5e1e4f05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.alter_column(
        "integration_attestation_publications", "check_run_id",
        existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=True,
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    # PostgreSQL refuses narrowing if out-of-range IDs remain; never truncate
    # evidence already published to GitHub.
    op.alter_column(
        "integration_attestation_publications", "check_run_id",
        existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=True,
    )
