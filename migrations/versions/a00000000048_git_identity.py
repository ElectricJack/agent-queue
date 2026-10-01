"""Project Git identity overrides and the launch identity of each session.

Revision ID: a00000000048
Revises: a00000000047
"""

import sqlalchemy as sa
from alembic import op

revision = "a00000000048"
down_revision = "a00000000047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    projects = {column["name"] for column in inspector.get_columns("projects")}
    for name in ("git_identity_name", "git_identity_email"):
        if name not in projects:
            op.add_column("projects", sa.Column(name, sa.Text(), nullable=True))
    checks = {check["name"] for check in sa.inspect(bind).get_check_constraints("projects")}
    if "ck_projects_git_identity_pair" not in checks:
        op.create_check_constraint(
            "ck_projects_git_identity_pair",
            "projects",
            "(git_identity_name IS NULL) = (git_identity_email IS NULL)",
        )
    sessions = {column["name"] for column in inspector.get_columns("sessions")}
    if "git_identity_digest" not in sessions:
        op.add_column("sessions", sa.Column("git_identity_digest", sa.Text(), nullable=True))
        # Every existing session was launched with an earlier release's
        # per-profile identity; mark it so a pool claim retires it rather
        # than handing it new work under that identity.
        op.execute("UPDATE sessions SET git_identity_digest = 'legacy'")


def downgrade() -> None:
    bind = op.get_bind()
    if "git_identity_digest" in {c["name"] for c in sa.inspect(bind).get_columns("sessions")}:
        op.drop_column("sessions", "git_identity_digest")
    checks = {check["name"] for check in sa.inspect(bind).get_check_constraints("projects")}
    if "ck_projects_git_identity_pair" in checks:
        op.drop_constraint("ck_projects_git_identity_pair", "projects", type_="check")
    projects = {column["name"] for column in sa.inspect(bind).get_columns("projects")}
    for name in ("git_identity_email", "git_identity_name"):
        if name in projects:
            op.drop_column("projects", name)
