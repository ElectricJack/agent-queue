"""candidate handoff workspace compatibility

Revision ID: 46f910d0dce6
Revises: e1eab6dbc186
Create Date: 2026-09-05 15:57:52.541433

"""
from collections.abc import Sequence
import importlib

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "46f910d0dce6"
down_revision: str | Sequence[str] | None = "e1eab6dbc186"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_resolution_guard() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """CREATE OR REPLACE FUNCTION integration_candidate_resolution_is_monotone()
            RETURNS trigger AS $$ BEGIN
            IF ROW(NEW.batch_id, NEW.revision, NEW.member_ordinal, NEW.operation_id,
              NEW.operation_episode_id, NEW.stage_ordinal, NEW.stage_deadline_at,
              NEW.project_id, NEW.repair_task_id, NEW.repair_session_id,
              NEW.repair_session_instance_token, NEW.repair_workspace_id,
              NEW.repair_workspace_path, NEW.repository_id, NEW.branch,
              NEW.target_branch, NEW.target_kind, NEW.fence_owner_id,
              NEW.fence_token, NEW.partial_head_sha, NEW.source_base_sha,
              NEW.source_head_sha, NEW.resolved_head_sha, NEW.resolved_tree_sha,
              NEW.repair_commit_shas::text) IS DISTINCT FROM
              ROW(OLD.batch_id, OLD.revision, OLD.member_ordinal, OLD.operation_id,
              OLD.operation_episode_id, OLD.stage_ordinal, OLD.stage_deadline_at,
              OLD.project_id, OLD.repair_task_id, OLD.repair_session_id,
              OLD.repair_session_instance_token, OLD.repair_workspace_id,
              OLD.repair_workspace_path, OLD.repository_id, OLD.branch,
              OLD.target_branch, OLD.target_kind, OLD.fence_owner_id,
              OLD.fence_token, OLD.partial_head_sha, OLD.source_base_sha,
              OLD.source_head_sha, OLD.resolved_head_sha, OLD.resolved_tree_sha,
              OLD.repair_commit_shas::text)
            THEN RAISE EXCEPTION 'candidate resolution identity is immutable'; END IF;
            IF OLD.handoff_owner_id IS NOT NULL AND
              ROW(NEW.handoff_owner_id, NEW.handoff_fence_token) IS DISTINCT FROM
              ROW(OLD.handoff_owner_id, OLD.handoff_fence_token)
            THEN RAISE EXCEPTION 'candidate resolution handoff is immutable'; END IF;
            IF (NEW.handoff_owner_id IS NULL) <> (NEW.handoff_fence_token IS NULL)
            THEN RAISE EXCEPTION 'candidate resolution handoff is incomplete'; END IF;
            IF OLD.push_evidence IS NOT NULL AND
              NEW.push_evidence::text IS DISTINCT FROM OLD.push_evidence::text
            THEN RAISE EXCEPTION 'candidate resolution push evidence is immutable'; END IF;
            IF (CASE NEW.state WHEN 'reserved' THEN 0 WHEN 'pushed' THEN 1 ELSE 2 END)
              NOT IN ((CASE OLD.state WHEN 'reserved' THEN 0 WHEN 'pushed' THEN 1 ELSE 2 END),
              (CASE OLD.state WHEN 'reserved' THEN 0 WHEN 'pushed' THEN 1 ELSE 2 END) + 1)
            THEN RAISE EXCEPTION 'candidate resolution transition is not adjacent'; END IF;
            RETURN NEW; END; $$ LANGUAGE plpgsql"""
        )
        return
    op.execute("DROP TRIGGER IF EXISTS trg_candidate_resolution_identity")
    columns = (
        "batch_id", "revision", "member_ordinal", "operation_id", "operation_episode_id",
        "stage_ordinal", "stage_deadline_at", "project_id", "repair_task_id",
        "repair_session_id", "repair_session_instance_token", "repair_workspace_id",
        "repair_workspace_path", "repository_id", "branch", "target_branch", "target_kind",
        "fence_owner_id", "fence_token", "partial_head_sha", "source_base_sha",
        "source_head_sha", "resolved_head_sha", "resolved_tree_sha", "repair_commit_shas",
    )
    identity = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns)
    op.execute(
        "CREATE TRIGGER trg_candidate_resolution_identity BEFORE UPDATE ON "
        f"integration_candidate_resolutions WHEN {identity} BEGIN SELECT RAISE(ABORT, "
        "'candidate resolution identity is immutable'); END"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_candidate_resolution_handoff")
    op.execute(
        "CREATE TRIGGER trg_candidate_resolution_handoff BEFORE UPDATE ON "
        "integration_candidate_resolutions WHEN (OLD.handoff_owner_id IS NOT NULL AND "
        "(NEW.handoff_owner_id IS NOT OLD.handoff_owner_id OR "
        "NEW.handoff_fence_token IS NOT OLD.handoff_fence_token)) OR "
        "((NEW.handoff_owner_id IS NULL) <> (NEW.handoff_fence_token IS NULL)) BEGIN "
        "SELECT RAISE(ABORT, 'candidate resolution handoff is immutable'); END"
    )


def upgrade() -> None:
    """Upgrade schema."""
    irreconstructible = op.get_bind().execute(
        sa.text(
            "SELECT COUNT(*) FROM integration_candidate_resolutions r "
            "LEFT JOIN workspaces w ON w.id = r.repair_workspace_id "
            "WHERE w.workspace_path IS NULL OR w.workspace_path = '' "
            "OR substr(w.workspace_path, 1, 1) <> '/' "
            "OR r.target_branch IS NULL OR r.branch IS NULL"
        )
    ).scalar_one()
    if irreconstructible:
        raise RuntimeError(
            "cannot upgrade candidate resolutions: irreconstructible legacy authority "
            "(missing absolute workspace path or publication target)"
        )
    op.add_column(
        "integration_candidate_resolutions",
        sa.Column("repair_workspace_path", sa.Text(), nullable=True),
    )
    op.add_column(
        "integration_candidate_resolutions",
        sa.Column("target_kind", sa.Text(), nullable=True),
    )
    op.add_column(
        "integration_candidate_resolutions",
        sa.Column("handoff_owner_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "integration_candidate_resolutions",
        sa.Column("handoff_fence_token", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE integration_candidate_resolutions SET repair_workspace_path = "
        "(SELECT workspace_path FROM workspaces WHERE id = repair_workspace_id), "
        "target_kind = CASE WHEN target_branch = branch THEN 'legacy_integration' "
        "ELSE 'qualified' END"
    )
    with op.batch_alter_table("integration_candidate_resolutions") as batch_op:
        batch_op.alter_column("repair_workspace_path", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("target_kind", existing_type=sa.Text(), nullable=False)
        batch_op.create_check_constraint(
            "ck_integration_candidate_resolutions_target_kind",
            "target_kind IN ('qualified', 'legacy_integration')",
        )
        batch_op.create_check_constraint(
            "ck_integration_candidate_resolutions_handoff",
            "(handoff_owner_id IS NULL AND handoff_fence_token IS NULL) OR "
            "(handoff_owner_id IS NOT NULL AND handoff_fence_token IS NOT NULL "
            "AND handoff_fence_token >= 0)",
        )
    _replace_resolution_guard()


def downgrade() -> None:
    """Downgrade schema."""
    live_handoffs = op.get_bind().execute(
        sa.text(
            "SELECT COUNT(*) FROM integration_candidate_resolutions "
            "WHERE handoff_owner_id IS NOT NULL OR handoff_fence_token IS NOT NULL"
        )
    ).scalar_one()
    if live_handoffs:
        raise RuntimeError(
            "cannot downgrade candidate resolutions with live candidate handoff provenance"
        )
    with op.batch_alter_table("integration_candidate_resolutions") as batch_op:
        batch_op.drop_constraint(
            "ck_integration_candidate_resolutions_handoff", type_="check"
        )
        batch_op.drop_constraint(
            "ck_integration_candidate_resolutions_target_kind", type_="check"
        )
        batch_op.drop_column("target_kind")
        batch_op.drop_column("repair_workspace_path")
        batch_op.drop_column("handoff_fence_token")
        batch_op.drop_column("handoff_owner_id")
    previous = importlib.import_module(
        "migrations.versions.e1eab6dbc186_candidate_durable_mutation_claims"
    )
    previous._replace_authority_guards()
