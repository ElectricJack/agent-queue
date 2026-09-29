"""Every project is bound to a router, and no project has a default profile.

Revision ID: a00000000043
Revises: a00000000042

Revision 3 of the mandatory-task-routing spec (2026-09-28, §8, §11; Task 8):

* binds every project still without a router to ``default-assignment-routing``
  and makes ``projects.assignment_playbook_id`` NOT NULL with that server
  default;
* sets ``projects.default_profile_id`` to NULL on every project and drops the
  column (its foreign key to ``agent_profiles`` goes with it).  Nothing reads
  it any more: the router routes every task.

Idempotent: the backfill touches only unbound rows, the NOT NULL and default
are altered only while missing, and the column is dropped only while present;
the squashed baseline builds from live metadata, which already has neither.
Downgrade makes the binding nullable again and restores an empty
``default_profile_id`` column: the dropped values are not recoverable, and
code at revision 42 treats NULL as "no default".
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000043"
down_revision = "a00000000042"
branch_labels = None
depends_on = None

# Literal copy of src.routing.sources.DEFAULT_ROUTER_PLAYBOOK_ID: a revision
# must not change meaning when the code it shipped with moves on.
_DEFAULT_ROUTER = "default-assignment-routing"


def _project_columns(bind) -> dict[str, dict]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns("projects")}


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE projects SET assignment_playbook_id = :router "
            "WHERE assignment_playbook_id IS NULL OR btrim(assignment_playbook_id) = ''"
        ),
        {"router": _DEFAULT_ROUTER},
    )
    binding = _project_columns(bind)["assignment_playbook_id"]
    if binding["nullable"] or not binding.get("default"):
        op.alter_column(
            "projects",
            "assignment_playbook_id",
            existing_type=sa.Text(),
            nullable=False,
            server_default=_DEFAULT_ROUTER,
        )
    if "default_profile_id" in _project_columns(bind):
        bind.execute(sa.text("UPDATE projects SET default_profile_id = NULL"))
        op.drop_column("projects", "default_profile_id")


def downgrade() -> None:
    bind = op.get_bind()
    columns = _project_columns(bind)
    if not columns["assignment_playbook_id"]["nullable"]:
        op.alter_column(
            "projects",
            "assignment_playbook_id",
            existing_type=sa.Text(),
            nullable=True,
            server_default=None,
        )
    if "default_profile_id" not in columns:
        op.add_column(
            "projects",
            sa.Column(
                "default_profile_id",
                sa.Text(),
                sa.ForeignKey("agent_profiles.id"),
                nullable=True,
            ),
        )
