"""Development delivery journal and rollout mode.

Revision ID: a0000000000c
Revises: a0000000000b
"""

from alembic import op
from sqlalchemy import CheckConstraint

revision = "a0000000000c"
down_revision = "a0000000000b"
branch_labels = None
depends_on = None


def _constraints(development):
    from src.database.tables import integration_rollout_transitions, projects

    for table in (projects, integration_rollout_transitions):
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and "'development'" in str(
                constraint.sqltext
            ):
                op.drop_constraint(constraint.name, table.name, type_="check")
                sql = str(constraint.sqltext)
                if not development:
                    sql = sql.replace(", 'development'", "")
                op.create_check_constraint(constraint.name, table.name, sql)


def upgrade():
    from src.database.tables import development_deliveries

    development_deliveries.create(op.get_bind(), checkfirst=True)
    _constraints(True)


def downgrade():
    # Refuse downgrade while development mode is configured; do not discard a live journal.
    from sqlalchemy import text

    if op.get_bind().scalar(text("SELECT EXISTS (SELECT 1 FROM development_deliveries)")):
        raise RuntimeError("development delivery history exists; retain the journal")
    if op.get_bind().scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM projects WHERE "
            "hierarchical_integration_mode = 'development' OR "
            "hierarchical_integration_desired_mode = 'development')"
        )
    ):
        raise RuntimeError("disable development integration before downgrade")
    # Historical rollout rows retain the new enum; only constrain project configuration.
    from src.database.tables import projects

    for name in ("hierarchical_integration_mode", "hierarchical_integration_desired_mode"):
        op.drop_constraint("ck_projects_" + name, projects.name, type_="check")
        op.create_check_constraint(
            "ck_projects_" + name,
            projects.name,
            name + " IN ('disabled', 'observe', 'hierarchy', 'train')",
        )
    op.drop_table("development_deliveries")
