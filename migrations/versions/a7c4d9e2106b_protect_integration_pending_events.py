"""Protect correctness-critical integration pending events.

Revision ID: a7c4d9e2106b
Revises: 3f30b34c7e7c
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c4d9e2106b"
down_revision: str | Sequence[str] | None = "3f30b34c7e7c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _restore_sqlite_outbox_guard() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute(
        "CREATE TRIGGER trg_integration_outbox_attempts_monotone "
        "BEFORE UPDATE ON integration_outbox "
        "WHEN NEW.attempts < OLD.attempts "
        "BEGIN SELECT RAISE(ABORT, "
        "'integration outbox attempts cannot decrease'); END"
    )


def _create_cursor_guard() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION integration_outbox_cursor_monotone() RETURNS trigger AS $$ "
            "BEGIN IF NEW.acceptance_cursor < OLD.acceptance_cursor THEN "
            "RAISE EXCEPTION 'integration outbox acceptance cursor cannot decrease'; "
            "END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_integration_outbox_cursor_monotone BEFORE UPDATE ON "
            "integration_outbox FOR EACH ROW EXECUTE FUNCTION "
            "integration_outbox_cursor_monotone()"
        )
        return
    op.execute(
        "CREATE TRIGGER trg_integration_outbox_cursor_monotone "
        "BEFORE UPDATE ON integration_outbox "
        "WHEN NEW.acceptance_cursor < OLD.acceptance_cursor "
        "BEGIN SELECT RAISE(ABORT, "
        "'integration outbox acceptance cursor cannot decrease'); END"
    )


def _drop_cursor_guard() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER trg_integration_outbox_cursor_monotone ON integration_outbox"
        )
        op.execute("DROP FUNCTION integration_outbox_cursor_monotone()")
        return
    op.execute("DROP TRIGGER trg_integration_outbox_cursor_monotone")


def upgrade() -> None:
    with op.batch_alter_table("playbook_pending_events") as batch:
        batch.add_column(sa.Column("activation_id", sa.Text(), nullable=True))
        batch.add_column(sa.Column("artifact_sha256", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("protected", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch.create_foreign_key(
            "fk_playbook_pending_events_artifact",
            "playbook_artifacts",
            ["artifact_sha256"],
            ["artifact_sha256"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("integration_outbox") as batch:
        batch.add_column(sa.Column("destination_manifest", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column("acceptance_cursor", sa.Integer(), server_default="0", nullable=False)
        )
        batch.create_check_constraint(
            "ck_integration_outbox_acceptance_cursor", "acceptance_cursor >= 0"
        )
    op.create_table(
        "integration_outbox_artifact_pins",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"], ["integration_outbox.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["artifact_sha256"],
            ["playbook_artifacts.artifact_sha256"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id", "artifact_sha256"),
    )
    op.create_index(
        "idx_integration_outbox_artifact_pins_sha",
        "integration_outbox_artifact_pins",
        ["artifact_sha256"],
    )
    _restore_sqlite_outbox_guard()
    _create_cursor_guard()


def downgrade() -> None:
    _drop_cursor_guard()
    op.drop_index(
        "idx_integration_outbox_artifact_pins_sha",
        table_name="integration_outbox_artifact_pins",
    )
    op.drop_table("integration_outbox_artifact_pins")
    with op.batch_alter_table("integration_outbox") as batch:
        batch.drop_constraint("ck_integration_outbox_acceptance_cursor", type_="check")
        batch.drop_column("acceptance_cursor")
        batch.drop_column("destination_manifest")
    _restore_sqlite_outbox_guard()
    with op.batch_alter_table("playbook_pending_events") as batch:
        batch.drop_constraint("fk_playbook_pending_events_artifact", type_="foreignkey")
        batch.drop_column("protected")
        batch.drop_column("artifact_sha256")
        batch.drop_column("activation_id")
