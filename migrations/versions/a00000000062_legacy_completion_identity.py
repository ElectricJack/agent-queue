"""Give missing-row legacy completions a lifecycle identity independent of edits.

Revision ID: a00000000062
Revises: a00000000061

Keep the old exact locator, including archives. For live completed tasks,
successful audited attestations after the last reopen can recover a locator
invalidated by an ordinary edit. An audit only locates the immutable Git record;
it never asserts containment. Reopened incarnations cannot borrow old decisions.
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "a00000000062"
down_revision = "a00000000061"
branch_labels = None
depends_on = None

COLUMN = "legacy_completion_id"
DEFAULT = sa.text("'legacy:' || gen_random_uuid()::text")
BATCH = 500


def _old_identity(row) -> str:
    material = json.dumps([
        row["project_id"], row["repository_id"], row["id"], row["updated_at"],
    ], separators=(",", ":"))
    return "legacy:" + hashlib.sha256(material.encode()).hexdigest()


def _audited_identity(row, metadata, audits) -> str | None:
    """Recover only an accepted decision for this live completed incarnation."""
    if row["status"] != "COMPLETED":
        return None
    try:
        rework = json.loads(metadata.get("integration_rework_at", "0"))
        current = json.loads(metadata.get("development_completion_id", "null"))
    except (TypeError, ValueError):
        return None
    if type(rework) not in (int, float):
        return None
    for audit in audits:
        if audit["project_id"] != row["project_id"] or (
            audit["timestamp"] <= max(rework, row["created_at"])
        ):
            continue
        try:
            results = json.loads(audit["payload"])["results"]
        except (TypeError, ValueError, KeyError):
            continue
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict) or (
                result.get("task_id") != row["id"] or result.get("error")
                or result.get("action") not in {"written", "present"}
                or current is not None and current != result.get("generation")
            ):
                continue
            generation = result.get("legacy_generation") or result.get("generation")
            if isinstance(generation, str) and generation.startswith("legacy:"):
                return generation
    return None


def _backfill(bind, table: str) -> None:
    """Bound memory and preserve initialized identities on repeated upgrades."""
    after = ""
    while True:
        rows = bind.execute(sa.text(f"""
            SELECT t.*, COALESCE(t.repo_id, p.integration_repository_id) AS repository_id
            FROM {table} t LEFT JOIN projects p ON p.id = t.project_id
            WHERE t.{COLUMN} IS NULL AND t.id > :after ORDER BY t.id LIMIT :limit
        """), {"after": after, "limit": BATCH}).mappings().all()
        if not rows:
            return
        metadata, audits = {}, {}
        if table == "tasks":
            ids = [row["id"] for row in rows]
            query = sa.text("""
                SELECT task_id, key, value FROM task_metadata WHERE task_id IN :ids
                AND key IN ('integration_rework_at', 'development_completion_id')
            """).bindparams(sa.bindparam("ids", expanding=True))
            for task_id, key, value in bind.execute(query, {"ids": ids}):
                metadata.setdefault(task_id, {})[key] = value
            query = sa.text("""
                SELECT task_id, project_id, timestamp, payload FROM events
                WHERE task_id IN :ids AND event_type = 'development.provenance_attested'
                ORDER BY timestamp DESC, id DESC
            """).bindparams(sa.bindparam("ids", expanding=True))
            for audit in bind.execute(query, {"ids": ids}).mappings():
                audits.setdefault(audit["task_id"], []).append(audit)
        values = [{"id": row["id"], "generation": _audited_identity(
            row, metadata.get(row["id"], {}), audits.get(row["id"], []),
        ) or _old_identity(row)} for row in rows]
        bind.execute(sa.text(f"""
            UPDATE {table} SET {COLUMN} = :generation WHERE id = :id AND {COLUMN} IS NULL
        """), values)
        after = rows[-1]["id"]


def upgrade() -> None:
    bind = op.get_bind()
    for table in ("tasks", "archived_tasks"):
        inspector = sa.inspect(bind)
        if not inspector.has_table(table):
            continue
        if COLUMN not in {column["name"] for column in inspector.get_columns(table)}:
            op.add_column(table, sa.Column(COLUMN, sa.Text(), nullable=True))
        _backfill(bind, table)
        op.alter_column(table, COLUMN, nullable=False, server_default=DEFAULT)


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("tasks", "archived_tasks"):
        inspector = sa.inspect(bind)
        if inspector.has_table(table) and COLUMN in {
            column["name"] for column in inspector.get_columns(table)
        }:
            op.drop_column(table, COLUMN)
