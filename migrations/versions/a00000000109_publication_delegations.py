"""Explicit, attempt-bound integration publication delegations."""

from alembic import op
import sqlalchemy as sa

revision = "a00000000109"
down_revision = "a00000000108"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("integration_publication_delegations"):
        op.create_table(
            "integration_publication_delegations",
            sa.Column("id", sa.Text, primary_key=True),
            sa.Column("project_id", sa.Text, nullable=False),
            sa.Column("repository_id", sa.Text, nullable=False),
            sa.Column("repository_url", sa.Text, nullable=False),
            sa.Column("task_id", sa.Text, nullable=False),
            sa.Column("session_id", sa.Text, nullable=False),
            sa.Column("session_instance_token", sa.Text, nullable=False),
            sa.Column("claim_epoch", sa.Integer, nullable=False),
            sa.Column("source_branch", sa.Text, nullable=False),
            sa.Column("target_branch", sa.Text, nullable=False),
            sa.Column("expected_target_oid", sa.Text, nullable=False),
            sa.Column("expected_owner_fence", sa.BigInteger, nullable=False),
            sa.Column("owner_fence", sa.BigInteger, nullable=False),
            sa.Column("ttl_seconds", sa.Integer, nullable=False),
            sa.Column("issued_by", sa.Text, nullable=False),
            sa.Column("reason", sa.Text, nullable=False),
            sa.Column("created_at", sa.Float, nullable=False),
            sa.Column("expires_at", sa.Float, nullable=False),
            sa.Column("revoked_at", sa.Float, nullable=True),
            sa.Column("revoked_by", sa.Text, nullable=True),
            sa.Column("revoke_reason", sa.Text, nullable=True),
            sa.Column("published_at", sa.Float, nullable=True),
            sa.Column("published_oid", sa.Text, nullable=True),
            sa.CheckConstraint(
                "claim_epoch >= 0 AND expected_owner_fence >= 0 AND owner_fence > 0",
                name="ck_integration_publication_delegations_fence",
            ),
            sa.CheckConstraint(
                "ttl_seconds BETWEEN 1 AND 480 AND expires_at = created_at + ttl_seconds",
                name="ck_integration_publication_delegations_expiry",
            ),
        )
        op.create_index("idx_integration_publication_delegations_task",
                        "integration_publication_delegations", ["task_id", "session_id"])


def downgrade():
    # Authority/audit evidence survives code rollback; old code cannot use grants.
    pass
