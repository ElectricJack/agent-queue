"""Name the conversation kind so a channel can carry exactly one.

Revision ID: a00000000062
Revises: a00000000061

Chat-extension spec §2.2: a top-level message joins "one durable conversation
row per channel", and §7.1 lists the new columns as additive,
inspector-guarded revisions. Existing rows are ``thread`` conversations --
the mention-routing model is unchanged -- so this revision writes no history,
renames nothing and drops nothing. The partial unique index is what stops a
channel from accumulating a second live conversation; a closed one is outside
the index so the next message opens a fresh row.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000062"
down_revision = "a00000000061"
branch_labels = None
depends_on = None

TABLE = "supervisor_conversations"
COLUMN = "kind"
CHECK = "ck_supervisor_conversations_kind"
CHANNEL_INDEX = "uq_supervisor_conversations_channel"
INDEX_PREDICATE = "kind = 'channel' AND state <> 'closed'"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return
    if COLUMN not in {column["name"] for column in inspector.get_columns(TABLE)}:
        op.add_column(
            TABLE,
            sa.Column(COLUMN, sa.Text(), nullable=False, server_default="thread"),
        )
    checks = {check["name"] for check in sa.inspect(bind).get_check_constraints(TABLE)}
    if CHECK not in checks:
        op.create_check_constraint(CHECK, TABLE, "kind IN ('thread','channel')")
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}
    if CHANNEL_INDEX not in indexes:
        op.create_index(
            CHANNEL_INDEX,
            TABLE,
            ["transport", "channel_id"],
            unique=True,
            postgresql_where=sa.text(INDEX_PREDICATE),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}
    if CHANNEL_INDEX in indexes:
        op.drop_index(CHANNEL_INDEX, table_name=TABLE)
    checks = {check["name"] for check in sa.inspect(bind).get_check_constraints(TABLE)}
    if CHECK in checks:
        op.drop_constraint(CHECK, TABLE, type_="check")
    columns = {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}
    if COLUMN in columns:
        op.drop_column(TABLE, COLUMN)
