"""Durable object-attached operator decisions.

Revision ID: a00000000077
Revises: a00000000076
"""
import sqlalchemy as sa
from alembic import op

revision = "a00000000077"
down_revision = "a00000000076"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("operator_decisions"):
        op.create_table(
            "operator_decisions",
            sa.Column("id", sa.Text(), primary_key=True),
            *[sa.Column(name, sa.Text(), nullable=False) for name in (
                "project_id", "object_kind", "object_id", "effect", "operator", "decision",
                "source", "source_ref", "recorded_by", "idempotency_key",
            )],
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("releases", sa.Text(), sa.ForeignKey("operator_decisions.id")),
            sa.CheckConstraint("object_kind IN ('task', 'batch', 'operation')",
                               name="ck_operator_decisions_object_kind"),
            sa.CheckConstraint("effect IN ('note', 'hold', 'release')",
                               name="ck_operator_decisions_effect"),
            sa.CheckConstraint("(effect = 'release') = (releases IS NOT NULL)",
                               name="ck_operator_decisions_release"),
            sa.UniqueConstraint("project_id", "idempotency_key",
                                name="uq_operator_decisions_request"),
            sa.UniqueConstraint("releases", name="uq_operator_decisions_release"),
        )
        op.create_index("ix_operator_decisions_object", "operator_decisions",
                        ["project_id", "object_kind", "object_id"])

    op.execute("""
        CREATE OR REPLACE FUNCTION operator_decisions_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'operator decisions are append-only; record a release';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger
                           WHERE tgname = 'operator_decisions_immutable'
                           AND tgrelid = 'operator_decisions'::regclass) THEN
                CREATE TRIGGER operator_decisions_immutable
                BEFORE UPDATE OR DELETE ON operator_decisions
                FOR EACH ROW EXECUTE FUNCTION operator_decisions_immutable();
            END IF;
        END $$
    """)


def downgrade():
    if sa.inspect(op.get_bind()).has_table("operator_decisions"):
        op.drop_table("operator_decisions")
    op.execute("DROP FUNCTION IF EXISTS operator_decisions_immutable()")
