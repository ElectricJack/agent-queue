"""Bind parent subjects to immutable episode identity, additively.

Revision ID: a00000000058
Revises: a00000000057

Existing subjects remain unbound and legacy-owned. No receipts are rewritten,
no parent is activated and no legacy table is deleted or backfilled.
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000058"
down_revision = "a00000000057"
branch_labels = None
depends_on = None

TABLE = "integration_subjects"
COLUMN = "parent_episode_id"
CHECK = "ck_integration_subjects_parent_episode"
FK = "fk_integration_subjects_parent_episode"
INDEX = "uq_integration_subjects_parent_episode"
TRIGGER = "integration_subject_parent_episode_pinned"
FUNCTION = "integration_subject_parent_episode_is_pinned"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if COLUMN not in {column["name"] for column in inspector.get_columns(TABLE)}:
        op.add_column(TABLE, sa.Column(COLUMN, sa.Text(), nullable=True))
    if CHECK not in {check["name"] for check in inspector.get_check_constraints(TABLE)}:
        op.create_check_constraint(
            CHECK, TABLE, "parent_episode_id IS NULL OR kind = 'parent_episode'"
        )
    if FK not in {fk["name"] for fk in inspector.get_foreign_keys(TABLE)}:
        op.create_foreign_key(
            FK,
            TABLE,
            "integration_parent_episodes",
            ["task_id", COLUMN],
            ["parent_task_id", "id"],
            ondelete="RESTRICT",
        )
    if INDEX not in {index["name"] for index in inspector.get_indexes(TABLE)}:
        op.create_index(
            INDEX,
            TABLE,
            [COLUMN],
            unique=True,
            postgresql_where=sa.text("parent_episode_id IS NOT NULL"),
        )
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {FUNCTION}() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.parent_episode_id IS NOT NULL
                AND NEW.parent_episode_id IS DISTINCT FROM OLD.parent_episode_id THEN
                RAISE EXCEPTION 'integration subject parent episode is immutable once bound';
            END IF;
            IF NEW.parent_episode_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM integration_parent_episodes e
                WHERE e.id = NEW.parent_episode_id AND e.parent_task_id = NEW.task_id
                    AND e.repository_id = NEW.repository_id
            ) THEN
                RAISE EXCEPTION 'integration subject parent episode identity mismatch';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON {TABLE}")
    op.execute(
        f"CREATE TRIGGER {TRIGGER} BEFORE INSERT OR UPDATE ON {TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON {TABLE}")
        if COLUMN in {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}:
            op.drop_column(TABLE, COLUMN)
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}()")
