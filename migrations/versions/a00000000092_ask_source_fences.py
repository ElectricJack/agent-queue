"""Source-owned human gate semantics and question submission fences.

H04 consumes revision 92; the transport package must chain after this revision.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000092"
down_revision = "a00000000091"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    for table, additions in {
        "gates": [sa.Column("human_request", JSONB, nullable=True)],
        "agent_questions": [
            sa.Column("ask_decision_id", sa.Text, nullable=True),
            sa.Column("submission_state", sa.Text, nullable=False, server_default="pending"),
        ],
    }.items():
        columns = {c["name"] for c in sa.inspect(bind).get_columns(table)}
        for column in additions:
            if column.name not in columns:
                op.add_column(table, column)
                if table == "agent_questions" and column.name == "submission_state":
                    # Historical answered rows predate the pre-submission fence.
                    # An absent receipt cannot establish whether input was sent.
                    op.execute(
                        sa.text(
                            "UPDATE agent_questions SET submission_state = 'unknown' "
                            "WHERE state = 'answered'"
                        )
                    )
                    op.execute(
                        sa.text(
                            "UPDATE agent_questions SET submission_state = 'delivered' "
                            "WHERE delivered_at IS NOT NULL"
                        )
                    )

    checks = {c["name"] for c in sa.inspect(bind).get_check_constraints("agent_questions")}
    if "ck_agent_question_submission_state" not in checks:
        op.create_check_constraint(
            "ck_agent_question_submission_state",
            "agent_questions",
            "submission_state IN ('pending','unknown','delivered')",
        )


def downgrade():
    raise RuntimeError("Human source receipts require a verified forward-compatible reader")
