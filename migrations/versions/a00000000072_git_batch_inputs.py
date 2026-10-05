"""Add compatible frozen Git inputs and batch intent.

Revision ID: a00000000072
Revises: a00000000071
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000072"
down_revision = "a00000000071"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    from src.database.tables import integration_batch_members, integration_batches

    for table in (integration_batches, integration_batch_members):
        table.create(bind, checkfirst=True)
    for name, columns in {
        "integration_batches": [
            sa.Column("target_ref", sa.Text(), nullable=True),
            sa.Column("intent", sa.Text(), nullable=False, server_default="open"),
            sa.Column("repair_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        ],
        "integration_batch_members": [sa.Column("source_sha", sa.Text(), nullable=True)],
    }.items():
        present = {column["name"] for column in sa.inspect(bind).get_columns(name)}
        for column in columns:
            if column.name not in present:
                op.add_column(name, column)
    op.alter_column("integration_batch_members", "review_evidence_id", nullable=True)
    for table, name, expression in (
        ("integration_batches", "ck_integration_batches_intent", "intent IN ('open','paused','aborted')"),
        ("integration_batches", "ck_integration_batches_repair_attempts", "repair_attempt_count >= 0"),
        ("integration_batches", "ck_integration_batches_target_ref",
         "target_ref IS NULL OR target_ref LIKE 'refs/heads/%'"),
        ("integration_batch_members", "ck_integration_batch_members_review_or_input",
         "source_sha IS NOT NULL OR review_evidence_id IS NOT NULL"),
    ):
        checks = {constraint["name"] for constraint in sa.inspect(bind).get_check_constraints(table)}
        if name not in checks:
            op.create_check_constraint(name, table, expression)
    # Keep legacy admission exclusive while independent Git targets can batch.
    op.execute("DROP INDEX IF EXISTS uq_integration_batches_active_project")
    index = next(i for i in integration_batches.indexes
                 if i.name == "uq_integration_batches_active_project")
    index.create(bind)
    op.execute("""
        CREATE OR REPLACE FUNCTION integration_git_batch_intent_guard() RETURNS trigger AS $$
        BEGIN
          IF OLD.target_ref IS NOT NULL THEN
            IF NEW.target_ref IS DISTINCT FROM OLD.target_ref THEN
              RAISE EXCEPTION 'frozen batch target is immutable';
            END IF;
            IF OLD.intent = 'aborted' AND NEW.intent <> 'aborted' THEN
              RAISE EXCEPTION 'aborted batch cannot reopen';
            END IF;
            IF NEW.repair_attempt_count < OLD.repair_attempt_count THEN
              RAISE EXCEPTION 'batch repair counter cannot decrease';
            END IF;
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_integration_git_batch_intent ON integration_batches")
    op.execute("""CREATE TRIGGER trg_integration_git_batch_intent
        BEFORE UPDATE ON integration_batches FOR EACH ROW
        EXECUTE FUNCTION integration_git_batch_intent_guard()""")


def downgrade():
    # Additive rollback is read-only. Legacy readers ignore these input columns;
    # never discard retained frozen sources or make an aborted batch runnable.
    pass
