"""candidate durable mutation claims

Revision ID: e1eab6dbc186
Revises: 69416e65ee21
Create Date: 2026-09-05 15:18:56.636179

"""
from collections.abc import Sequence
import importlib

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1eab6dbc186"
down_revision: str | Sequence[str] | None = "69416e65ee21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_authority_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """CREATE OR REPLACE FUNCTION integration_candidate_publication_is_monotone()
            RETURNS trigger AS $$ BEGIN
            IF ROW(NEW.repository_id, NEW.repository_numeric_id,
              NEW.repository_full_name, NEW.base_ref, NEW.head_ref, NEW.head_sha,
              NEW.expected_old_sha, NEW.idempotency_key) IS DISTINCT FROM
              ROW(OLD.repository_id, OLD.repository_numeric_id,
              OLD.repository_full_name, OLD.base_ref, OLD.head_ref, OLD.head_sha,
              OLD.expected_old_sha, OLD.idempotency_key)
            THEN RAISE EXCEPTION 'candidate publication identity is immutable'; END IF;
            IF OLD.state = 'pr_published' AND
              ROW(NEW.pr_number, NEW.pr_url) IS DISTINCT FROM ROW(OLD.pr_number, OLD.pr_url)
            THEN RAISE EXCEPTION 'candidate PR identity is immutable'; END IF;
            IF (CASE NEW.state WHEN 'reserved' THEN 0 WHEN 'ref_published' THEN 1
              WHEN 'pr_reserved' THEN 2 ELSE 3 END) NOT IN
              ((CASE OLD.state WHEN 'reserved' THEN 0 WHEN 'ref_published' THEN 1
              WHEN 'pr_reserved' THEN 2 ELSE 3 END),
              (CASE OLD.state WHEN 'reserved' THEN 0 WHEN 'ref_published' THEN 1
              WHEN 'pr_reserved' THEN 2 ELSE 3 END) + 1)
            THEN RAISE EXCEPTION 'candidate publication transition is not adjacent'; END IF;
            RETURN NEW; END; $$ LANGUAGE plpgsql"""
        )
        op.execute(
            """CREATE OR REPLACE FUNCTION integration_candidate_resolution_is_monotone()
            RETURNS trigger AS $$ BEGIN
            IF ROW(NEW.batch_id, NEW.revision, NEW.member_ordinal, NEW.operation_id,
              NEW.operation_episode_id, NEW.stage_ordinal, NEW.stage_deadline_at,
              NEW.project_id, NEW.repair_task_id, NEW.repair_session_id,
              NEW.repair_session_instance_token, NEW.repair_workspace_id,
              NEW.repository_id, NEW.branch, NEW.target_branch, NEW.fence_owner_id,
              NEW.fence_token, NEW.partial_head_sha, NEW.source_base_sha,
              NEW.source_head_sha, NEW.resolved_head_sha, NEW.resolved_tree_sha,
              NEW.repair_commit_shas::text) IS DISTINCT FROM
              ROW(OLD.batch_id, OLD.revision, OLD.member_ordinal, OLD.operation_id,
              OLD.operation_episode_id, OLD.stage_ordinal, OLD.stage_deadline_at,
              OLD.project_id, OLD.repair_task_id, OLD.repair_session_id,
              OLD.repair_session_instance_token, OLD.repair_workspace_id,
              OLD.repository_id, OLD.branch, OLD.target_branch, OLD.fence_owner_id,
              OLD.fence_token, OLD.partial_head_sha, OLD.source_base_sha,
              OLD.source_head_sha, OLD.resolved_head_sha, OLD.resolved_tree_sha,
              OLD.repair_commit_shas::text)
            THEN RAISE EXCEPTION 'candidate resolution identity is immutable'; END IF;
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
    for trigger in (
        "trg_candidate_publication_identity",
        "trg_candidate_publication_state",
        "trg_candidate_resolution_identity",
        "trg_candidate_resolution_state",
        "trg_candidate_resolution_push_immutable",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    publication_identity = " OR ".join(
        f"NEW.{column} IS NOT OLD.{column}"
        for column in (
            "repository_id", "repository_numeric_id", "repository_full_name", "base_ref",
            "head_ref", "head_sha", "expected_old_sha", "idempotency_key",
        )
    )
    op.execute(
        "CREATE TRIGGER trg_candidate_publication_identity BEFORE UPDATE ON "
        f"integration_candidate_publications WHEN {publication_identity} OR "
        "(OLD.state = 'pr_published' AND (NEW.pr_number IS NOT OLD.pr_number OR "
        "NEW.pr_url IS NOT OLD.pr_url)) BEGIN SELECT RAISE(ABORT, "
        "'candidate publication identity is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_candidate_publication_state BEFORE UPDATE ON "
        "integration_candidate_publications WHEN (CASE NEW.state WHEN 'reserved' THEN 0 "
        "WHEN 'ref_published' THEN 1 WHEN 'pr_reserved' THEN 2 ELSE 3 END) NOT IN "
        "((CASE OLD.state WHEN 'reserved' THEN 0 WHEN 'ref_published' THEN 1 "
        "WHEN 'pr_reserved' THEN 2 ELSE 3 END), (CASE OLD.state WHEN 'reserved' THEN 0 "
        "WHEN 'ref_published' THEN 1 WHEN 'pr_reserved' THEN 2 ELSE 3 END) + 1) BEGIN "
        "SELECT RAISE(ABORT, 'candidate publication transition is not adjacent'); END"
    )
    resolution_identity = " OR ".join(
        f"NEW.{column} IS NOT OLD.{column}"
        for column in (
            "batch_id", "revision", "member_ordinal", "operation_id", "operation_episode_id",
            "stage_ordinal", "stage_deadline_at", "project_id",
            "repair_task_id", "repair_session_id", "repair_session_instance_token",
            "repair_workspace_id", "repository_id", "branch", "target_branch",
            "fence_owner_id", "fence_token", "partial_head_sha", "source_base_sha",
            "source_head_sha", "resolved_head_sha", "resolved_tree_sha", "repair_commit_shas",
        )
    )
    op.execute(
        "CREATE TRIGGER trg_candidate_resolution_identity BEFORE UPDATE ON "
        f"integration_candidate_resolutions WHEN {resolution_identity} BEGIN "
        "SELECT RAISE(ABORT, 'candidate resolution identity is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_candidate_resolution_state BEFORE UPDATE ON "
        "integration_candidate_resolutions WHEN (CASE NEW.state WHEN 'reserved' THEN 0 "
        "WHEN 'pushed' THEN 1 ELSE 2 END) NOT IN ((CASE OLD.state WHEN 'reserved' THEN 0 "
        "WHEN 'pushed' THEN 1 ELSE 2 END), (CASE OLD.state WHEN 'reserved' THEN 0 "
        "WHEN 'pushed' THEN 1 ELSE 2 END) + 1) BEGIN SELECT RAISE(ABORT, "
        "'candidate resolution transition is not adjacent'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_candidate_resolution_push_immutable BEFORE UPDATE ON "
        "integration_candidate_resolutions WHEN OLD.push_evidence IS NOT NULL AND "
        "NEW.push_evidence IS NOT OLD.push_evidence BEGIN SELECT RAISE(ABORT, "
        "'candidate resolution push evidence is immutable'); END"
    )


def _create_mutation_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """CREATE FUNCTION integration_candidate_mutation_is_monotone()
            RETURNS trigger AS $$ BEGIN
            IF ROW(NEW.batch_id, NEW.revision, NEW.member_ordinal, NEW.resolution_id,
              NEW.purpose, NEW.repository_id, NEW.branch, NEW.target_branch,
              NEW.expected_old_sha, NEW.desired_sha, NEW.operation_id,
              NEW.operation_episode_id, NEW.operation_stage, NEW.lease_owner_id,
              NEW.lease_fence_token, NEW.branch_owner_id, NEW.branch_owner_role,
              NEW.branch_fence_token) IS DISTINCT FROM
              ROW(OLD.batch_id, OLD.revision, OLD.member_ordinal, OLD.resolution_id,
              OLD.purpose, OLD.repository_id, OLD.branch, OLD.target_branch,
              OLD.expected_old_sha, OLD.desired_sha, OLD.operation_id,
              OLD.operation_episode_id, OLD.operation_stage, OLD.lease_owner_id,
              OLD.lease_fence_token, OLD.branch_owner_id, OLD.branch_owner_role,
              OLD.branch_fence_token)
            THEN RAISE EXCEPTION 'candidate mutation identity is immutable'; END IF;
            IF OLD.state = 'applied' AND ROW(NEW.state, NEW.remote_sha, NEW.nonce,
              NEW.expires_at) IS DISTINCT FROM ROW(OLD.state, OLD.remote_sha, OLD.nonce,
              OLD.expires_at)
            THEN RAISE EXCEPTION 'applied candidate mutation is immutable'; END IF;
            RETURN NEW; END; $$ LANGUAGE plpgsql"""
        )
        op.execute(
            "CREATE TRIGGER trg_candidate_mutation_monotone BEFORE UPDATE ON "
            "integration_candidate_ref_mutations FOR EACH ROW EXECUTE FUNCTION "
            "integration_candidate_mutation_is_monotone()"
        )
        return
    identity = " OR ".join(
        f"NEW.{column} IS NOT OLD.{column}"
        for column in (
            "batch_id", "revision", "member_ordinal", "resolution_id", "purpose",
            "repository_id", "branch", "target_branch", "expected_old_sha", "desired_sha",
            "operation_id", "operation_episode_id", "operation_stage", "lease_owner_id",
            "lease_fence_token", "branch_owner_id", "branch_owner_role", "branch_fence_token",
        )
    )
    op.execute(
        "CREATE TRIGGER trg_candidate_mutation_identity BEFORE UPDATE ON "
        f"integration_candidate_ref_mutations WHEN {identity} BEGIN SELECT RAISE(ABORT, "
        "'candidate mutation identity is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_candidate_mutation_applied BEFORE UPDATE ON "
        "integration_candidate_ref_mutations WHEN OLD.state = 'applied' AND "
        "(NEW.state IS NOT OLD.state OR NEW.remote_sha IS NOT OLD.remote_sha OR "
        "NEW.nonce IS NOT OLD.nonce OR NEW.expires_at IS NOT OLD.expires_at) BEGIN "
        "SELECT RAISE(ABORT, 'applied candidate mutation is immutable'); END"
    )


def upgrade() -> None:
    """Upgrade schema."""
    irreconstructible = op.get_bind().execute(
        sa.text(
            "SELECT COUNT(*) FROM integration_candidate_resolutions r "
            "LEFT JOIN integration_repair_operations o ON o.id = r.operation_id "
            "LEFT JOIN integration_repair_stages s ON s.operation_id = r.operation_id "
            "AND s.ordinal = r.stage_ordinal "
            "LEFT JOIN integration_batches b ON b.id = r.batch_id "
            "WHERE o.episode_id IS NULL OR s.deadline_at IS NULL OR b.project_id IS NULL"
        )
    ).scalar_one()
    if irreconstructible:
        raise RuntimeError(
            "cannot upgrade candidate resolutions: irreconstructible legacy authority "
            "(missing operation episode, stage deadline, or project)"
        )
    op.add_column(
        "integration_candidate_resolutions",
        sa.Column("operation_episode_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "integration_candidate_resolutions",
        sa.Column("stage_deadline_at", sa.Float(), nullable=True),
    )
    op.add_column(
        "integration_candidate_resolutions", sa.Column("project_id", sa.Text(), nullable=True)
    )
    op.add_column(
        "integration_candidate_resolutions", sa.Column("target_branch", sa.Text(), nullable=True)
    )
    op.execute("UPDATE integration_candidate_resolutions SET target_branch = branch")
    op.execute(
        "UPDATE integration_candidate_resolutions SET "
        "operation_episode_id = (SELECT episode_id FROM integration_repair_operations "
        "WHERE id = integration_candidate_resolutions.operation_id), "
        "stage_deadline_at = (SELECT deadline_at FROM integration_repair_stages WHERE "
        "operation_id = integration_candidate_resolutions.operation_id AND ordinal = "
        "integration_candidate_resolutions.stage_ordinal), "
        "project_id = (SELECT project_id FROM integration_batches WHERE "
        "id = integration_candidate_resolutions.batch_id)"
    )
    with op.batch_alter_table("integration_candidate_resolutions") as batch_op:
        batch_op.alter_column("operation_episode_id", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("stage_deadline_at", existing_type=sa.Float(), nullable=False)
        batch_op.alter_column("project_id", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("target_branch", existing_type=sa.Text(), nullable=False)
    op.create_table(
        "integration_candidate_ref_mutations",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("member_ordinal", sa.Integer(), nullable=True),
        sa.Column("resolution_id", sa.Text(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("target_branch", sa.Text(), nullable=False),
        sa.Column("expected_old_sha", sa.Text(), nullable=False),
        sa.Column("desired_sha", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("operation_episode_id", sa.Text(), nullable=False),
        sa.Column("operation_stage", sa.Integer(), nullable=False),
        sa.Column("lease_owner_id", sa.Text(), nullable=False),
        sa.Column("lease_fence_token", sa.Integer(), nullable=False),
        sa.Column("branch_owner_id", sa.Text(), nullable=False),
        sa.Column("branch_owner_role", sa.Text(), nullable=False),
        sa.Column("branch_fence_token", sa.Integer(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("remote_sha", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_integration_candidate_ref_mutations_revision"),
        sa.CheckConstraint(
            "member_ordinal IS NULL OR member_ordinal >= 0",
            name="ck_integration_candidate_ref_mutations_member",
        ),
        sa.CheckConstraint(
            "purpose IN ('candidate_final', 'candidate_partial', 'repair_resolution', "
            "'repair_handoff')",
            name="ck_integration_candidate_ref_mutations_purpose",
        ),
        sa.CheckConstraint(
            "operation_stage IN (0, 1)", name="ck_integration_candidate_ref_mutations_stage"
        ),
        sa.CheckConstraint(
            "lease_fence_token >= 0 AND branch_fence_token >= 0",
            name="ck_integration_candidate_ref_mutations_fences",
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'applied')",
            name="ck_integration_candidate_ref_mutations_state",
        ),
        sa.CheckConstraint(
            "(state = 'reserved' AND remote_sha IS NULL) OR "
            "(state = 'applied' AND remote_sha = desired_sha)",
            name="ck_integration_candidate_ref_mutations_remote",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id", "revision"],
            ["integration_candidate_revisions.batch_id", "integration_candidate_revisions.revision"],
            name="fk_integration_candidate_ref_mutations_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolution_id"],
            ["integration_candidate_resolutions.id"],
            name="fk_integration_candidate_ref_mutations_resolution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    _replace_authority_guards()
    _create_mutation_guards()


def downgrade() -> None:
    """Downgrade schema."""
    live = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM integration_candidate_ref_mutations")
    ).scalar_one()
    if live:
        raise RuntimeError("cannot downgrade candidate mutation claims while rows exist")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER trg_candidate_mutation_monotone ON integration_candidate_ref_mutations"
        )
        op.execute("DROP FUNCTION integration_candidate_mutation_is_monotone()")
    op.drop_table("integration_candidate_ref_mutations")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER trg_candidate_resolution_monotone ON integration_candidate_resolutions"
        )
        op.execute(
            "DROP TRIGGER trg_candidate_publication_monotone ON integration_candidate_publications"
        )
        op.execute("DROP FUNCTION integration_candidate_resolution_is_monotone()")
        op.execute("DROP FUNCTION integration_candidate_publication_is_monotone()")
    else:
        for trigger in (
            "trg_candidate_publication_identity",
            "trg_candidate_publication_state",
            "trg_candidate_resolution_identity",
            "trg_candidate_resolution_state",
            "trg_candidate_resolution_push_immutable",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    with op.batch_alter_table("integration_candidate_resolutions") as batch_op:
        batch_op.drop_column("target_branch")
        batch_op.drop_column("project_id")
        batch_op.drop_column("stage_deadline_at")
        batch_op.drop_column("operation_episode_id")
    previous = importlib.import_module(
        "migrations.versions.69416e65ee21_candidate_publication_authority"
    )
    previous._create_guards()
