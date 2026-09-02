"""Task graph layout tables (spatial-layout design §4.10).

Revision ID: d1e2f3a4b5c6
Revises: 009793fbb800
"""

from alembic import op
import sqlalchemy as sa

revision = "d1e2f3a4b5c6"
down_revision = "009793fbb800"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_layouts",
        sa.Column("project_id", sa.Text(), sa.ForeignKey("projects.id"), nullable=False, primary_key=True),
        sa.Column("variant", sa.Text(), nullable=False, primary_key=True),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.id"), nullable=False, primary_key=True),
        sa.Column("container_id", sa.Text(), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("order_key", sa.Text(), nullable=False),
        sa.Column("w", sa.Float(), nullable=False),
        sa.Column("h", sa.Float(), nullable=False),
        sa.Column("rel_x", sa.Float(), nullable=False),
        sa.Column("rel_y", sa.Float(), nullable=False),
        sa.Column("abs_x", sa.Float(), nullable=False),
        sa.Column("abs_y", sa.Float(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("agg_children", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agg_descendants", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agg_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agg_running", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agg_blocked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agg_active", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("variant IN ('all', 'active')", name="ck_task_layouts_variant"),
        sa.CheckConstraint("kind IN ('card', 'container', 'stub')", name="ck_task_layouts_kind"),
    )
    op.create_index("idx_task_layouts_path", "task_layouts", ["project_id", "variant", "path"])
    op.create_index("idx_task_layouts_depth", "task_layouts", ["project_id", "variant", "depth"])
    op.create_index(
        "idx_task_layouts_container", "task_layouts", ["project_id", "variant", "container_id"]
    )

    op.create_table(
        "task_layout_cells",
        sa.Column("project_id", sa.Text(), nullable=False, primary_key=True),
        sa.Column("variant", sa.Text(), nullable=False, primary_key=True),
        sa.Column("cell_x", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("cell_y", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("task_id", sa.Text(), nullable=False, primary_key=True),
    )
    op.create_index(
        "idx_task_layout_cells_cell",
        "task_layout_cells",
        ["project_id", "variant", "cell_x", "cell_y"],
    )
    op.create_index(
        "idx_task_layout_cells_task", "task_layout_cells", ["project_id", "variant", "task_id"]
    )

    op.create_table(
        "project_layout_meta",
        sa.Column("project_id", sa.Text(), sa.ForeignKey("projects.id"), nullable=False, primary_key=True),
        sa.Column("variant", sa.Text(), nullable=False, primary_key=True),
        sa.Column("layout_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extent_w", sa.Float(), nullable=False, server_default="0"),
        sa.Column("extent_h", sa.Float(), nullable=False, server_default="0"),
        sa.Column("node_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("reconciled_at", sa.Float(), nullable=True),
    )

    op.create_table(
        "layout_dirty",
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("idx_layout_dirty_project", "layout_dirty", ["project_id", "seq"])

    op.create_table(
        "layout_jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("variant", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("idx_layout_jobs_project_status", "layout_jobs", ["project_id", "status"])


def downgrade():
    op.drop_index("idx_layout_jobs_project_status", table_name="layout_jobs")
    op.drop_table("layout_jobs")

    op.drop_index("idx_layout_dirty_project", table_name="layout_dirty")
    op.drop_table("layout_dirty")

    op.drop_table("project_layout_meta")

    op.drop_index("idx_task_layout_cells_task", table_name="task_layout_cells")
    op.drop_index("idx_task_layout_cells_cell", table_name="task_layout_cells")
    op.drop_table("task_layout_cells")

    op.drop_index("idx_task_layouts_container", table_name="task_layouts")
    op.drop_index("idx_task_layouts_depth", table_name="task_layouts")
    op.drop_index("idx_task_layouts_path", table_name="task_layouts")
    op.drop_table("task_layouts")
