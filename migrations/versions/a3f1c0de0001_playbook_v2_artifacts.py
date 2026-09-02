"""playbook v2: content-addressed artifacts and explicit activations

Revision ID: a3f1c0de0001
Revises: d3e7b1c9a204
Create Date: 2026-09-01

Additive: two new tables, no existing table touched.  Ordered first in the
Package 3 chain because playbook_v2_runs.artifact_sha256 references
playbook_artifacts (roadmap section 7 / child plan section 4.3).
"""

import sqlalchemy as sa
from alembic import op

revision = "a3f1c0de0001"
down_revision = "d3e7b1c9a204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "playbook_artifacts",
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("playbook_id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), server_default="'system'", nullable=False),
        sa.Column("scope_identifier", sa.Text(), server_default="''", nullable=False),
        sa.Column("schema_generation", sa.Integer(), server_default="2", nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source_digest", sa.Text(), nullable=False),
        sa.Column("contract_fingerprint", sa.Text(), nullable=False),
        sa.Column("profile_fingerprint", sa.Text(), server_default="''", nullable=False),
        sa.Column("compiler_build", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("validation", sa.Text(), server_default="'{}'", nullable=False),
        sa.Column("compiled_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("artifact_sha256"),
        sa.CheckConstraint(
            "scope IN ('system', 'project', 'agent_type', 'supervisor')",
            name="ck_playbook_artifacts_scope",
        ),
    )
    op.create_index(
        "idx_playbook_artifacts_playbook", "playbook_artifacts", ["playbook_id", "version"]
    )
    op.create_index("idx_playbook_artifacts_source", "playbook_artifacts", ["source_digest"])
    op.create_index("idx_playbook_artifacts_created", "playbook_artifacts", ["created_at"])

    op.create_table(
        "playbook_activations",
        sa.Column("activation_id", sa.Text(), nullable=False),
        sa.Column("playbook_id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), server_default="'system'", nullable=False),
        sa.Column("scope_identifier", sa.Text(), server_default="''", nullable=False),
        sa.Column("active_artifact_sha256", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("health", sa.Text(), server_default="'disabled'", nullable=False),
        sa.Column("reasons", sa.Text(), server_default="'[]'", nullable=False),
        sa.Column("activated_at", sa.Float(), nullable=True),
        sa.Column("activated_by", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["active_artifact_sha256"],
            ["playbook_artifacts.artifact_sha256"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("activation_id"),
        sa.UniqueConstraint(
            "playbook_id", "scope", "scope_identifier", name="uq_playbook_activations_scope"
        ),
        sa.CheckConstraint(
            "health IN ('ready', 'question_required', 'invalid', 'disabled', "
            "'stale_contract', 'unavailable')",
            name="ck_playbook_activations_health",
        ),
    )
    op.create_index("idx_playbook_activations_health", "playbook_activations", ["health"])


def downgrade() -> None:
    op.drop_index("idx_playbook_activations_health", table_name="playbook_activations")
    op.drop_table("playbook_activations")
    op.drop_index("idx_playbook_artifacts_created", table_name="playbook_artifacts")
    op.drop_index("idx_playbook_artifacts_source", table_name="playbook_artifacts")
    op.drop_index("idx_playbook_artifacts_playbook", table_name="playbook_artifacts")
    op.drop_table("playbook_artifacts")
