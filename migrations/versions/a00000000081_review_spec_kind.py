"""Preserve design/implementation classification on document revisions."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000081"
down_revision = "a00000000080"
branch_labels = None
depends_on = None

TABLE = "doc_review_revisions"
CONSTRAINT = "ck_doc_review_revisions_spec_kind"


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if "spec_kind" not in {col["name"] for col in sa.inspect(bind).get_columns(TABLE)}:
        op.add_column(TABLE, sa.Column("spec_kind", sa.Text(), nullable=True))
    if CONSTRAINT not in {
        item["name"] for item in sa.inspect(bind).get_check_constraints(TABLE)
    }:
        op.create_check_constraint(
            CONSTRAINT, TABLE, "spec_kind IS NULL OR spec_kind IN ('design', 'implementation')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    if CONSTRAINT in {item["name"] for item in sa.inspect(bind).get_check_constraints(TABLE)}:
        op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    if "spec_kind" in {col["name"] for col in sa.inspect(bind).get_columns(TABLE)}:
        op.drop_column(TABLE, "spec_kind")
