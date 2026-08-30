"""Make global supervisor sessions, history, and token accounting projectless.

Revision ID: 5f37c424acde
Revises: d4e5f6a7b8c9
Create Date: 2026-08-30

Only the former supervisor placeholder is removable: preserve any customized
project settings and every remaining hard or soft project reference.
"""

import time
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "5f37c424acde"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Freeze the defaults used by the legacy Project(id="global", name="Global")
# call, rather than importing application models that later revisions can change.
_PLACEHOLDER_SETTINGS = {
    "name": "Global",
    "credit_weight": 1.0,
    "max_concurrent_agents": 2,
    "status": "ACTIVE",
    "total_tokens_used": 0,
    "budget_limit": None,
    "workspace_path": None,
    "discord_channel_id": None,
    "discord_control_channel_id": None,
    "repo_url": "",
    "repo_default_branch": "main",
    "default_profile_id": None,
}


def _alter_project_nullability(nullable: bool) -> None:
    bind = op.get_bind()
    # SQLite must rebuild messages, whose reply_to_id points back to messages.
    # Disable enforcement outside a transaction for that rebuild, restoring the
    # connection's setting afterwards. Otherwise DROP TABLE rejects reply chains.
    foreign_keys = (
        bind.dialect.name == "sqlite" and bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    )
    if foreign_keys:
        with op.get_context().autocommit_block():
            bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        for table_name in ("messages", "sessions", "token_ledger"):
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.alter_column("project_id", existing_type=sa.Text(), nullable=nullable)
    finally:
        if foreign_keys:
            with op.get_context().autocommit_block():
                bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def _remove_unused_placeholder(bind: sa.engine.Connection) -> None:
    project = bind.execute(sa.text("SELECT * FROM projects WHERE id = 'global'")).mappings().first()
    if project is None:
        return
    settings = {key: value for key, value in project.items() if key not in ("id", "created_at")}
    if settings != _PLACEHOLDER_SETTINGS:
        return

    inspector = sa.inspect(bind)
    for table_name in inspector.get_table_names():
        # Include soft references such as archived tasks, events, scoped workspace
        # kinds, and API tokens; absence of an FK does not make them disposable.
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        references = {"project_id"} if "project_id" in columns else set()
        for fk in inspector.get_foreign_keys(table_name):
            if fk["referred_table"] == "projects" and fk["referred_columns"] == ["id"]:
                references.update(fk["constrained_columns"])
        if not references:
            continue
        table = sa.table(table_name, *(sa.column(name) for name in references))
        has_reference = (
            sa.select(sa.literal(1))
            .select_from(table)
            .where(sa.or_(*(table.c[name] == "global" for name in references)))
            .limit(1)
        )
        if bind.execute(has_reference).first() is not None:
            return

    bind.execute(sa.text("DELETE FROM projects WHERE id = 'global'"))


def upgrade() -> None:
    _alter_project_nullability(True)
    bind = op.get_bind()
    # Ledger agent_id is a soft reference and supervisor charges use session IDs.
    # Capture their legacy scope before detaching the sessions themselves.
    bind.execute(
        sa.text(
            "UPDATE token_ledger SET project_id = NULL WHERE project_id = 'global' "
            "AND agent_id IN (SELECT id FROM sessions WHERE project_id = 'global' "
            "AND name = 'n-supervisor--global')"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE sessions SET project_id = NULL "
            "WHERE project_id = 'global' AND name = 'n-supervisor--global'"
        )
    )
    # Replies may identify the runtime by UUID and omit its public address and
    # dashboard thread. Follow both directions of reply links, but never cross
    # a project's boundary. UNION (not UNION ALL) terminates even cyclic chains.
    bind.execute(
        sa.text("""
        WITH RECURSIVE global_messages AS (
            SELECT id, from_id, to_id, thread_id, reply_to_id
            FROM messages WHERE project_id = 'global'
        ), links (source, target) AS (
            SELECT child.id, parent.id
            FROM global_messages child JOIN global_messages parent
                ON child.reply_to_id = parent.id
            UNION ALL
            SELECT parent.id, child.id
            FROM global_messages child JOIN global_messages parent
                ON child.reply_to_id = parent.id
        ), conversation (id) AS (
            SELECT id FROM global_messages
            WHERE from_id IN ('supervisor-global', 'n-supervisor--global')
                OR to_id IN ('supervisor-global', 'n-supervisor--global')
                OR thread_id = 'dashboard:global'
            UNION
            SELECT links.target FROM links
                JOIN conversation ON links.source = conversation.id
        )
        UPDATE messages SET project_id = NULL
        WHERE project_id = 'global' AND id IN (SELECT id FROM conversation)
    """)
    )
    _remove_unused_placeholder(bind)


def downgrade() -> None:
    bind = op.get_bind()
    # The old schema requires a project for every row, including new projectless
    # history and accounting. Reuse any real global project without overwriting its settings.
    has_projectless_rows = any(
        bind.execute(
            sa.text(f"SELECT 1 FROM {table_name} WHERE project_id IS NULL LIMIT 1")
        ).first()
        is not None
        for table_name in ("sessions", "messages", "token_ledger")
    )
    if has_projectless_rows:
        existing = bind.execute(sa.text("SELECT id FROM projects WHERE id = 'global'")).first()
        if existing is None:
            bind.execute(
                sa.text(
                    "INSERT INTO projects (id, name, created_at) VALUES ('global', 'Global', :now)"
                ),
                {"now": time.time()},
            )
        for table_name in ("sessions", "messages", "token_ledger"):
            bind.execute(
                sa.text(f"UPDATE {table_name} SET project_id = 'global' WHERE project_id IS NULL")
            )
    _alter_project_nullability(False)
