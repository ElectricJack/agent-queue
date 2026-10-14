"""Shared human input admission; H06 extends this transport revision.

The original allocation of 92 moved to 93 because H04 added source fences at 92.
Cleanup must follow this transport revision, not replace it. Data backfill is a
bounded CommandHandler operation; this migration performs no external I/O.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000098"
down_revision = "a00000000097"
branch_labels = None
depends_on = None


def upgrade():
    from src.database.tables import (
        conversation_inputs, ask_external_bindings, human_communication_modes,
        ask_action_tokens, outbound_deliveries,
    )

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in (human_communication_modes, ask_external_bindings, ask_action_tokens):
        table.create(bind, checkfirst=True)
    existing = {c["name"] for c in inspector.get_columns("outbound_deliveries")}
    for name in (
        "ask_id", "binding_id", "project_id", "decision_generation", "display_version",
        "mode_generation", "root_generation", "send_kind", "write_started_at", "attempt_token",
    ):
        if name not in existing:
            op.add_column("outbound_deliveries", outbound_deliveries.c[name]._copy())
    checks = {c["name"] for c in inspector.get_check_constraints("outbound_deliveries")}
    for constraint in outbound_deliveries.constraints:
        if isinstance(constraint, sa.CheckConstraint) and constraint.name not in checks:
            op.create_check_constraint(constraint.name, "outbound_deliveries", str(constraint.sqltext))
    for index in outbound_deliveries.indexes:
        index.create(bind, checkfirst=True)
    columns = {c["name"] for c in inspector.get_columns("conversation_inputs")}
    additions = {
        "ask_id",
        "project_id",
        "routing_state",
        "routing_evidence",
        "receipt",
        "guild_id",
        "reference_message_id",
        "external_thread_id",
        "source_version",
        "retention_entry_id",
    }
    for name in additions - columns:
        op.add_column("conversation_inputs", conversation_inputs.c[name]._copy())
    op.alter_column("conversation_inputs", "conversation_id", nullable=True)
    for fk in inspector.get_foreign_keys("conversation_inputs"):
        if fk["constrained_columns"] == ["conversation_id"]:
            if fk["options"].get("ondelete") == "SET NULL":
                break
            op.drop_constraint(fk["name"], "conversation_inputs", type_="foreignkey")
            op.create_foreign_key(
                "fk_conversation_inputs_conversation",
                "conversation_inputs",
                "supervisor_conversations",
                ["conversation_id"],
                ["id"],
                ondelete="SET NULL",
            )
            break
    checks = {c["name"] for c in sa.inspect(bind).get_check_constraints("conversation_inputs")}
    for constraint in conversation_inputs.constraints:
        if isinstance(constraint, sa.CheckConstraint) and constraint.name in {
            "ck_conversation_inputs_char_count",
            "ck_conversation_inputs_source",
            "ck_conversation_inputs_routing_state",
        }:
            if constraint.name in checks:
                op.drop_constraint(constraint.name, "conversation_inputs", type_="check")
            op.create_check_constraint(
                constraint.name, "conversation_inputs", str(constraint.sqltext)
            )
    for index in conversation_inputs.indexes:
        index.create(bind, checkfirst=True)


def downgrade():
    raise RuntimeError("Verified human input requires a verified forward-compatible reader")
