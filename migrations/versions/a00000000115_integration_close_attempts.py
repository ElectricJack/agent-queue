"""Retain author-assisted close proposals across uncertain publication."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000115"
down_revision = "a00000000114"
branch_labels = depends_on = None


def _wait_kinds(include_close):
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("wait_records"):
        return
    name = "ck_wait_records_condition_kind"
    constraints = {c["name"]: c for c in inspector.get_check_constraints("wait_records")}
    if name in constraints:
        op.drop_constraint(name, "wait_records", type_="check")
    kinds = ["task", "dependency", "job", "ci_check", "ci_run", "pull_request", "batch",
             "message", "human", "timer", "resource", "playbook_condition"]
    if include_close:
        kinds.append("close_merge")
    op.create_check_constraint(name, "wait_records",
                               "condition_kind IN (" + ",".join(repr(k) for k in kinds) + ")")


def upgrade():
    _wait_kinds(True)
    if sa.inspect(op.get_bind()).has_table("integration_close_attempts"):
        return
    op.create_table(
        "integration_close_attempts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("project_id", sa.Text(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        *[sa.Column(name, sa.Text(), nullable=False) for name in (
            "session_id", "repository_id", "target_task_id", "target_ref", "source_oid",
            "candidate_ref", "state")],
        sa.Column("claim_epoch", sa.Integer(), nullable=False),
        *[sa.Column(name, sa.Text()) for name in (
            "base_oid", "candidate_oid", "candidate_path", "policy_digest", "budget_wait_id",
            "last_failure")],
        *[sa.Column(name, sa.Integer(), nullable=False, server_default="0")
          for name in ("version", "attempts")],
        *[sa.Column(name, JSONB(), nullable=False) for name in ("evidence", "close_args")],
        *[sa.Column(name, sa.Float(), nullable=False) for name in ("created_at", "updated_at")],
        sa.UniqueConstraint("task_id", "session_id", "claim_epoch", "source_oid", "target_ref",
                            name="uq_integration_close_generation"),
        sa.CheckConstraint("state IN ('preparing','needs_author','checking','publish_uncertain',"
                           "'finished','deferred','cancelled')", name="ck_integration_close_state"),
        sa.CheckConstraint("version >= 0 AND attempts >= 0", name="ck_integration_close_counters"),
    )


def downgrade():
    _wait_kinds(False)
    if sa.inspect(op.get_bind()).has_table("integration_close_attempts"):
        op.drop_table("integration_close_attempts")
