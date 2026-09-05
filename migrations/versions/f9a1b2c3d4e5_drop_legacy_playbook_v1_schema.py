"""Drop the retired Playbook V1 persistence schema.

The V1 runtime and its metadata declarations were removed during the V2
cutover, but its tables and two foreign keys remained in the Alembic chain.
That left a database upgraded to head different from ``tables.py`` and made
every subsequent autogenerate include an unrelated cleanup.

V1 run IDs cannot be translated to V2 artifact-pinned run IDs.  Routing and
workflow rows that reference them are therefore retired with the V1 history;
tasks are detached from their retired workflows before those rows are removed.
The data retirement is forward-only.  Downgrade reconstructs an empty legacy
schema solely for Alembic chain validation and refuses to discard V2 rows.

Revision ID: f9a1b2c3d4e5
Revises: e6a1b2c3d4f5
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "e6a1b2c3d4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V1_RUN_TABLE = "playbook_runs"
_RETARGETS = (
    ("task_assignment_routes", "playbook_run_id", "CASCADE"),
    ("workflows", "playbook_run_id", None),
)
_SQLITE_FK_NAMES = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def _run_fk(
    bind: sa.engine.Connection, table: str, column: str, referred_table: str
) -> dict | None:
    for foreign_key in sa.inspect(bind).get_foreign_keys(table):
        if (
            foreign_key.get("referred_table") == referred_table
            and list(foreign_key.get("constrained_columns") or []) == [column]
        ):
            return foreign_key
    return None


def _retarget_run_fk(
    table: str, column: str, from_table: str, to_table: str, ondelete: str | None
) -> None:
    """Point a V1-owned relationship at durable V2 runs on both dialects."""
    bind = op.get_bind()
    old_fk = _run_fk(bind, table, column, from_table)
    if old_fk is None:
        return

    new_name = f"fk_{table}_{column}_{to_table}"
    options = {"ondelete": ondelete} if ondelete else {}
    if bind.dialect.name == "sqlite":
        # SQLite does not preserve names for inline foreign keys.  Supplying a
        # convention gives the reflected legacy constraint a stable name for
        # batch_alter_table's copy-and-rebuild operation.
        legacy_name = f"fk_{table}_{column}_{from_table}"
        with op.batch_alter_table(table, naming_convention=_SQLITE_FK_NAMES) as batch:
            batch.drop_constraint(legacy_name, type_="foreignkey")
            batch.create_foreign_key(new_name, to_table, [column], ["run_id"], **options)
        return

    old_name = old_fk.get("name")
    if not old_name:
        raise RuntimeError(f"unnamed PostgreSQL foreign key on {table}.{column}")
    op.drop_constraint(old_name, table, type_="foreignkey")
    op.create_foreign_key(new_name, table, to_table, [column], ["run_id"], **options)


def upgrade() -> None:
    bind = op.get_bind()

    # These rows are necessarily V1-owned: the old foreign keys made a V2 run
    # ID invalid.  Clear task references first so the workflows deletion also
    # works with FK enforcement enabled.
    bind.execute(sa.text("UPDATE tasks SET workflow_id = NULL WHERE workflow_id IS NOT NULL"))
    bind.execute(sa.text("DELETE FROM task_assignment_routes"))
    bind.execute(sa.text("DELETE FROM workflows"))

    for table, column, ondelete in _RETARGETS:
        _retarget_run_fk(table, column, _V1_RUN_TABLE, "playbook_v2_runs", ondelete)

    with op.batch_alter_table("playbook_activations") as batch:
        batch.drop_constraint("ck_playbook_activations_review_evidence", type_="check")
        batch.drop_column("reviewed_at")
        batch.drop_column("reviewed_by")
        batch.drop_column("reviewed_artifact_sha256")

    op.drop_table("playbook_migration_acks")
    op.drop_table("playbook_cutover_events")
    op.drop_table(_V1_RUN_TABLE)


def downgrade() -> None:
    bind = op.get_bind()
    for table, _, _ in _RETARGETS:
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError(
                "cannot restore Playbook V1 foreign keys while V2 route/workflow rows exist"
            )

    op.create_table(
        _V1_RUN_TABLE,
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("playbook_id", sa.Text(), nullable=False),
        sa.Column("playbook_version", sa.Integer(), nullable=False),
        sa.Column("trigger_event", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("current_node", sa.Text()),
        sa.Column("conversation_history", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("node_trace", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.Float()),
        sa.Column("error", sa.Text()),
        sa.Column("pinned_graph", sa.Text()),
        sa.Column("paused_at", sa.Float()),
        sa.Column("waiting_for_event", sa.Text()),
        sa.Column("event_id", sa.Text()),
        sa.CheckConstraint(
            "status IN ('running', 'paused', 'completed', 'failed', 'timed_out', 'cancelled')",
            name="ck_playbook_runs_status",
        ),
    )
    op.create_index("idx_playbook_runs_playbook_id", _V1_RUN_TABLE, ["playbook_id"])
    op.create_index("idx_playbook_runs_status", _V1_RUN_TABLE, ["status"])
    op.create_index(
        "uq_playbook_runs_pb_event",
        _V1_RUN_TABLE,
        ["playbook_id", "event_id"],
        unique=True,
        sqlite_where=sa.text("event_id IS NOT NULL"),
        postgresql_where=sa.text("event_id IS NOT NULL"),
    )
    op.create_table(
        "playbook_cutover_events",
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("at", sa.Float(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default="{}"),
        sa.CheckConstraint(
            "kind IN ('v1_admission_closed', 'v1_admission_reopened', 'drain_completed', "
            "'cutover_authorized', 'switched_to_v2', 'rolled_back_to_v1', "
            "'window_coverage_rehearsal', 'rollback_window_closed')",
            name="ck_playbook_cutover_events_kind",
        ),
    )
    op.create_index(
        "idx_playbook_cutover_events_kind_at", "playbook_cutover_events", ["kind", "at"]
    )
    op.create_table(
        "playbook_migration_acks",
        sa.Column("playbook_id", sa.Text(), primary_key=True),
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("scope_identifier", sa.Text(), primary_key=True, server_default=""),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("acknowledged_by", sa.Text(), nullable=False),
        sa.Column("acknowledged_at", sa.Float(), nullable=False),
        sa.CheckConstraint("length(reason) >= 12", name="ck_playbook_migration_acks_reason"),
    )
    op.create_index(
        "idx_playbook_migration_acks_source", "playbook_migration_acks", ["source_sha256"]
    )

    for table, column, ondelete in _RETARGETS:
        _retarget_run_fk(table, column, "playbook_v2_runs", _V1_RUN_TABLE, ondelete)

    review_evidence = (
        "(reviewed_artifact_sha256 IS NULL AND reviewed_by IS NULL AND reviewed_at IS NULL) "
        "OR (scope = 'project' AND active_artifact_sha256 IS NOT NULL "
        "AND reviewed_artifact_sha256 IS NOT NULL AND reviewed_by IS NOT NULL "
        "AND reviewed_at IS NOT NULL AND reviewed_artifact_sha256 = active_artifact_sha256 "
        "AND length(trim(reviewed_by)) > 0)"
    )
    with op.batch_alter_table("playbook_activations") as batch:
        batch.add_column(sa.Column("reviewed_artifact_sha256", sa.Text()))
        batch.add_column(sa.Column("reviewed_by", sa.Text()))
        batch.add_column(sa.Column("reviewed_at", sa.Float()))
        batch.create_check_constraint("ck_playbook_activations_review_evidence", review_evidence)
