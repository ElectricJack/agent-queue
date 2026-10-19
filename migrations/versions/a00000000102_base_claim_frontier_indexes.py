"""Keep base-priority claims index ordered while the wait runtime is inactive."""

import sqlalchemy as sa
from alembic import op

revision = "a00000000102"
down_revision = "a00000000101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("tasks"):
        return
    existing = {index["name"] for index in inspector.get_indexes("tasks")}
    for name, columns, predicate in (
        (
            "idx_tasks_base_claim_frontier",
            ["project_id", "status", "is_blocked", "priority", "created_at"],
            None,
        ),
        (
            "idx_tasks_base_claim_frontier_by_profile",
            ["project_id", "profile_id", "status", "is_blocked", "priority", "created_at"],
            None,
        ),
        (
            "idx_tasks_base_claim_frontier_by_affinity",
            ["affinity_agent_id", "project_id", "status", "is_blocked", "priority", "created_at"],
            "affinity_agent_id IS NOT NULL",
        ),
    ):
        if name not in existing:
            op.create_index(
                name, "tasks", columns,
                postgresql_where=sa.text(predicate) if predicate else None,
            )


def downgrade() -> None:
    # Retain additive indexes across code rollback, as with the wait schema.
    return
