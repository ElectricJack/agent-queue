"""playbook cutover events

Playbook V2 Package 7 §6
(``docs/superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md``).
One additive, append-only audit table.  No data migration and no backfill: a
fleet that has not begun the cutover has no events, and "no events" is the
correct description of that state.

``CREATE TABLE`` only — no column is added to an existing table, so there is
no PostgreSQL rewrite and no SQLite table rebuild, and ``batch_alter_table``
is needed in neither direction.  ``kind`` is ``Text`` plus a named
``CheckConstraint`` rather than a PostgreSQL enum, matching every other status
column in this schema (``ck_playbook_runs_status``); ``at`` is ``Float`` epoch
seconds, matching ``playbook_runs.started_at``; ``detail`` is a JSON string in
``Text``, matching ``playbook_runs.trigger_event``.

Revision ID: d4e7c9a1f2b8
Revises: c7f4d1a2b3e0
Create Date: 2026-09-03

"""

import sqlalchemy as sa
from alembic import op

revision = "d4e7c9a1f2b8"
down_revision = "c7f4d1a2b3e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
            "'switched_to_v2', 'rolled_back_to_v1', 'window_coverage_rehearsal', "
            "'rollback_window_closed')",
            name="ck_playbook_cutover_events_kind",
        ),
    )
    op.create_index(
        "idx_playbook_cutover_events_kind_at",
        "playbook_cutover_events",
        ["kind", "at"],
    )


def downgrade() -> None:
    op.drop_index("idx_playbook_cutover_events_kind_at", table_name="playbook_cutover_events")
    op.drop_table("playbook_cutover_events")
