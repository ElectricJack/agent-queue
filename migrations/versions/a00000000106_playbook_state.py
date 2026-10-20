"""Durable playbook state, tombstones and retained operation journals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, Float, ForeignKey,
                        Integer, Table, Text, UniqueConstraint, false)
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000106"
down_revision = "a00000000105"
branch_labels = None
depends_on = None
metadata = sa.MetaData()

# Durable namespaces deliberately have no FK to mutable playbook/source rows.
playbook_state_namespaces = Table(
    "playbook_state_namespaces", metadata,
    Column("namespace_id", Text, primary_key=True),
    Column("scope_kind", Text, nullable=False),
    Column("scope_owner", Text, nullable=False),
    Column("playbook_id", Text, nullable=False),
    Column("state_schema_version", Integer, nullable=False),
    Column("revision", BigInteger, nullable=False, server_default="0"),
    Column("key_count", Integer, nullable=False, server_default="0"),
    Column("value_bytes", BigInteger, nullable=False, server_default="0"),
    Column("journal_bytes", BigInteger, nullable=False, server_default="0"),
    Column("replay_floor", Float),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("scope_kind", "scope_owner", "playbook_id", "state_schema_version",
                     name="uq_playbook_state_namespace_identity"),
    CheckConstraint("state_schema_version > 0", name="ck_playbook_state_namespace_version"),
    CheckConstraint("scope_kind IN ('system', 'project', 'profile') AND scope_owner <> '' "
                    "AND (scope_kind <> 'system' OR scope_owner = 'system') AND playbook_id <> ''",
                    name="ck_playbook_state_namespace_scope"),
    CheckConstraint("revision >= 0 AND key_count >= 0 AND value_bytes >= 0 AND journal_bytes >= 0",
                    name="ck_playbook_state_namespace_accounting"),
)

playbook_state_entries = Table(
    "playbook_state_entries", metadata,
    Column("namespace_id", Text, ForeignKey("playbook_state_namespaces.namespace_id"),
           primary_key=True),
    Column("record_space", Text, primary_key=True),
    Column("key", Text, primary_key=True),
    Column("value_reference", JSONB),
    Column("value_bytes", BigInteger, nullable=False, server_default="0"),
    Column("revision", BigInteger, nullable=False),
    Column("tombstone", Boolean, nullable=False, server_default=false()),
    Column("expires_at", Float),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("episode", BigInteger, nullable=False, server_default="0"),
    Column("active", Boolean, nullable=False, server_default=false()),
    Column("source_partition", JSONB),
    Column("first_seen", Float),
    Column("last_seen", Float),
    Column("last_notified", Float),
    Column("last_observation_id", Text),
    Column("last_observed_at", Float),
    Column("message_id", Text),
    Column("replay_floor", Float),
    CheckConstraint("record_space IN ('kv', 'notification') AND key <> ''",
                    name="ck_playbook_state_entry_identity"),
    CheckConstraint("revision > 0 AND value_bytes >= 0 AND episode >= 0",
                    name="ck_playbook_state_entry_counters"),
    CheckConstraint("record_space <> 'notification' OR expires_at IS NULL",
                    name="ck_playbook_state_notification_no_expiry"),
    CheckConstraint("NOT tombstone OR NOT active", name="ck_playbook_state_tombstone_inactive"),
)

playbook_state_operations = Table(
    "playbook_state_operations", metadata,
    Column("namespace_id", Text, ForeignKey("playbook_state_namespaces.namespace_id"),
           primary_key=True),
    Column("operation_key", Text, primary_key=True),
    Column("request_digest", Text, nullable=False),
    Column("result_reference", JSONB, nullable=False),
    Column("byte_count", BigInteger, nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("message_id", Text),
    Column("committed_at", Float, nullable=False),
    CheckConstraint("operation_key <> '' AND byte_count >= 0 AND revision > 0",
                    name="ck_playbook_state_operation_identity"),
)


def upgrade() -> None:
    bind = op.get_bind()
    for table in (playbook_state_namespaces, playbook_state_entries, playbook_state_operations):
        table.create(bind, checkfirst=True)


def downgrade() -> None:
    # Disabling admission must not discard suppression, revisions or replay history.
    pass
