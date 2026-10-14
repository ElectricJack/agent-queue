"""Source-owned human gate semantics and question submission fences.

H04 consumes revision 92; the transport package must chain after this revision.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000097"
down_revision = "a00000000096"
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
        if not sa.inspect(bind).has_table(table):
            continue
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

    if not sa.inspect(bind).has_table("agent_questions"):
        return
    checks = {c["name"] for c in sa.inspect(bind).get_check_constraints("agent_questions")}
    if "ck_agent_question_submission_state" not in checks:
        op.create_check_constraint(
            "ck_agent_question_submission_state",
            "agent_questions",
            "submission_state IN ('pending','unknown','delivered')",
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Check every retained fence before changing any schema. An empty install
    # can roll back; request semantics, decisions and delivery evidence cannot.
    for table, predicates in {
        "gates": {"human_request": "human_request IS NOT NULL"},
        "agent_questions": {
            "ask_decision_id": "ask_decision_id IS NOT NULL",
            "submission_state": "submission_state <> 'pending'",
        },
        "ask_decisions": {},
    }.items():
        if not inspector.has_table(table):
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        conditions = [predicate for column, predicate in predicates.items() if column in columns]
        if predicates and not conditions:
            continue
        where = " WHERE " + " OR ".join(conditions) if conditions else ""
        if bind.execute(sa.text(f"SELECT 1 FROM {table}{where} LIMIT 1")).first():
            raise RuntimeError(
                "Human source receipts require a verified forward-compatible reader"
            )
    if inspector.has_table("agent_questions"):
        checks = {c["name"] for c in inspector.get_check_constraints("agent_questions")}
        if "ck_agent_question_submission_state" in checks:
            op.drop_constraint("ck_agent_question_submission_state", "agent_questions", type_="check")
    for table, names in {
        "agent_questions": ("submission_state", "ask_decision_id"),
        "gates": ("human_request",),
    }.items():
        if inspector.has_table(table):
            columns = {c["name"] for c in inspector.get_columns(table)}
            for name in names:
                if name in columns:
                    op.drop_column(table, name)
