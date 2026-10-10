"""Cancel withdrawn review gates and preserve withdrawal provenance."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000090"
down_revision = "a00000000089"
branch_labels = None
depends_on = None

AUDIT_COLUMNS = {
    "withdrawn_by": sa.Text(),
    "withdrawn_at": sa.Float(),
    "withdrawn_via": sa.Text(),
    "withdrawal_reason": sa.Text(),
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("doc_reviews"):
        columns = {col["name"] for col in inspector.get_columns("doc_reviews")}
        for name, type_ in AUDIT_COLUMNS.items():
            if name not in columns:
                op.add_column("doc_reviews", sa.Column(name, type_, nullable=True))
        # Old records cannot tell us the surface. Do not invent one.
        op.execute(sa.text("""
            UPDATE doc_reviews SET withdrawn_by = decided_by, withdrawn_at = decided_at,
                withdrawal_reason = decision_note
            WHERE state = 'withdrawn' AND withdrawn_at IS NULL
        """))
    if inspector.has_table("gates"):
        if "ck_gates_status" in {
            item["name"] for item in inspector.get_check_constraints("gates")
        }:
            op.drop_constraint("ck_gates_status", "gates", type_="check")
        op.create_check_constraint(
            "ck_gates_status", "gates",
            "status IN ('open','resolved','expired','cancelled')",
        )
        if inspector.has_table("doc_reviews"):
            op.execute(sa.text("""
                UPDATE gates g SET status = 'cancelled', resolved_by = r.withdrawn_by,
                    resolution = 'review_withdrawn'
                FROM doc_reviews r WHERE r.gate_id = g.id AND g.gate_type = 'review'
                    AND g.await_id = r.id AND r.state = 'withdrawn'
                    AND g.status IN ('open', 'expired')
            """))
            if inspector.has_table("task_gates") and inspector.has_table("task_metadata"):
                op.execute(sa.text("""
                    INSERT INTO task_metadata (task_id, key, value)
                    SELECT DISTINCT tg.task_id, 'needs_attention', '"review_withdrawn"'
                    FROM task_gates tg JOIN gates g ON g.id = tg.gate_id
                    JOIN doc_reviews r ON r.gate_id = g.id
                    WHERE r.state = 'withdrawn' AND g.gate_type = 'review'
                        AND g.await_id = r.id AND g.status = 'cancelled'
                    ON CONFLICT (task_id, key) DO NOTHING
                """))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("gates"):
        op.execute(sa.text("UPDATE gates SET status = 'open' WHERE status = 'cancelled'"))
        op.drop_constraint("ck_gates_status", "gates", type_="check")
        op.create_check_constraint(
            "ck_gates_status", "gates", "status IN ('open','resolved','expired')",
        )
    if inspector.has_table("doc_reviews"):
        columns = {col["name"] for col in inspector.get_columns("doc_reviews")}
        for name in reversed(AUDIT_COLUMNS):
            if name in columns:
                op.drop_column("doc_reviews", name)
