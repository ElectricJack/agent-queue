"""Immutable protected observation snapshots and finite pages."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, Float, ForeignKey,
                        Index, Integer, Table, Text)
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000105"
down_revision = "a00000000104"
branch_labels = None
depends_on = None

# Frozen schema, independent of future application metadata.
metadata = sa.MetaData()
observation_snapshots = Table(
    "observation_snapshots", metadata,
    Column("snapshot_id", Text, primary_key=True),
    Column("producer", Text, nullable=False),
    Column("producer_version", Integer, nullable=False),
    Column("authority_fingerprint", Text, nullable=False),
    Column("scope_kind", Text, nullable=False),
    Column("scope_owner", Text, nullable=False),
    Column("run_id", Text),
    Column("request_hash", Text, nullable=False),
    Column("observed_at", Float, nullable=False),
    Column("materialized_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("manifest", JSONB, nullable=False),
    Column("coverage", JSONB, nullable=False),
    Column("envelope", JSONB, nullable=False),
    Column("complete", Boolean, nullable=False),
    Column("page_count", Integer, nullable=False),
    Column("content_digest", Text, nullable=False),
    Column("byte_count", BigInteger, nullable=False),
    CheckConstraint("producer_version > 0", name="ck_observation_snapshots_version"),
    CheckConstraint("scope_kind IN ('system', 'project', 'profile') AND scope_owner <> ''",
                    name="ck_observation_snapshots_scope"),
    CheckConstraint("page_count >= 0 AND byte_count >= 0", name="ck_observation_snapshots_sizes"),
    CheckConstraint("expires_at > materialized_at", name="ck_observation_snapshots_expiry"),
    Index("idx_observation_snapshots_expiry", "expires_at"),
    Index("idx_observation_snapshots_scope", "scope_kind", "scope_owner"),
    Index("idx_observation_snapshots_run", "run_id"),
)

observation_snapshot_pages = Table(
    "observation_snapshot_pages", metadata,
    Column("snapshot_id", Text, ForeignKey("observation_snapshots.snapshot_id", ondelete="CASCADE"),
           primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("token_hash", Text, nullable=False, unique=True),
    Column("private_reference", JSONB, nullable=False),
    Column("content_digest", Text, nullable=False),
    Column("byte_count", BigInteger, nullable=False),
    Column("row_count", Integer, nullable=False),
    Column("artifact", Boolean, nullable=False),
    CheckConstraint("ordinal >= 0 AND byte_count >= 0 AND row_count >= 0",
                    name="ck_observation_snapshot_pages_sizes"),
)


def upgrade() -> None:
    bind = op.get_bind()
    for table in (observation_snapshots, observation_snapshot_pages):
        table.create(bind, checkfirst=True)
    constraints = {c["name"]: c["sqltext"] for c in
                   sa.inspect(bind).get_check_constraints("playbook_private_values")}
    name = "ck_playbook_private_values_owner_kind"
    if "snapshot" not in constraints.get(name, ""):
        if name in constraints:
            op.drop_constraint(name, "playbook_private_values", type_="check")
        op.create_check_constraint(name, "playbook_private_values",
                                   "owner_kind IN ('run', 'namespace', 'snapshot')")


def downgrade() -> None:
    # Rollback disables admission; preserve encrypted evidence until expiry.
    pass
