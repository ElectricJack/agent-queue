"""candidate publication authority

Revision ID: 69416e65ee21
Revises: 9b3e5a7c1d20
Create Date: 2026-09-05 14:33:44.630097

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "69416e65ee21"
down_revision: str | Sequence[str] | None = "9b3e5a7c1d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _guard_names() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    collisions = existing & {
        "integration_candidate_publications",
        "integration_candidate_resolutions",
    }
    if collisions:
        raise RuntimeError(
            "candidate authority table name collision: " + ", ".join(sorted(collisions))
        )


def _create_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """CREATE FUNCTION integration_candidate_publication_is_monotone()
            RETURNS trigger AS $$ BEGIN
            IF ROW(NEW.repository_id, NEW.repository_numeric_id,
              NEW.repository_full_name, NEW.base_ref, NEW.head_ref, NEW.head_sha,
              NEW.expected_old_sha, NEW.idempotency_key) IS DISTINCT FROM
              ROW(OLD.repository_id, OLD.repository_numeric_id,
              OLD.repository_full_name, OLD.base_ref, OLD.head_ref, OLD.head_sha,
              OLD.expected_old_sha, OLD.idempotency_key)
            THEN RAISE EXCEPTION 'candidate publication identity is immutable'; END IF;
            IF (CASE NEW.state WHEN 'reserved' THEN 0 WHEN 'ref_published' THEN 1
              WHEN 'pr_reserved' THEN 2 ELSE 3 END) <
              (CASE OLD.state WHEN 'reserved' THEN 0 WHEN 'ref_published' THEN 1
              WHEN 'pr_reserved' THEN 2 ELSE 3 END)
            THEN RAISE EXCEPTION 'candidate publication state cannot regress'; END IF;
            RETURN NEW; END; $$ LANGUAGE plpgsql"""
        )
        op.execute(
            "CREATE TRIGGER trg_candidate_publication_monotone BEFORE UPDATE ON "
            "integration_candidate_publications FOR EACH ROW EXECUTE FUNCTION "
            "integration_candidate_publication_is_monotone()"
        )
        op.execute(
            """CREATE FUNCTION integration_candidate_resolution_is_monotone()
            RETURNS trigger AS $$ BEGIN
            IF ROW(NEW.batch_id, NEW.revision, NEW.member_ordinal, NEW.operation_id,
              NEW.stage_ordinal, NEW.repair_task_id, NEW.repair_session_id,
              NEW.repair_session_instance_token, NEW.repair_workspace_id,
              NEW.repository_id, NEW.branch, NEW.fence_owner_id, NEW.fence_token,
              NEW.partial_head_sha, NEW.source_base_sha, NEW.source_head_sha,
              NEW.resolved_head_sha, NEW.resolved_tree_sha,
              NEW.repair_commit_shas::text) IS DISTINCT FROM
              ROW(OLD.batch_id, OLD.revision, OLD.member_ordinal, OLD.operation_id,
              OLD.stage_ordinal, OLD.repair_task_id, OLD.repair_session_id,
              OLD.repair_session_instance_token, OLD.repair_workspace_id,
              OLD.repository_id, OLD.branch, OLD.fence_owner_id, OLD.fence_token,
              OLD.partial_head_sha, OLD.source_base_sha, OLD.source_head_sha,
              OLD.resolved_head_sha, OLD.resolved_tree_sha,
              OLD.repair_commit_shas::text)
            THEN RAISE EXCEPTION 'candidate resolution identity is immutable'; END IF;
            IF OLD.push_evidence IS NOT NULL AND
              NEW.push_evidence::text IS DISTINCT FROM OLD.push_evidence::text
            THEN RAISE EXCEPTION 'candidate resolution push evidence is immutable'; END IF;
            IF (CASE NEW.state WHEN 'reserved' THEN 0 WHEN 'pushed' THEN 1 ELSE 2 END) <
              (CASE OLD.state WHEN 'reserved' THEN 0 WHEN 'pushed' THEN 1 ELSE 2 END)
            THEN RAISE EXCEPTION 'candidate resolution state cannot regress'; END IF;
            RETURN NEW; END; $$ LANGUAGE plpgsql"""
        )
        op.execute(
            "CREATE TRIGGER trg_candidate_resolution_monotone BEFORE UPDATE ON "
            "integration_candidate_resolutions FOR EACH ROW EXECUTE FUNCTION "
            "integration_candidate_resolution_is_monotone()"
        )
        return
    publication_identity = " OR ".join(
        f"NEW.{column} IS NOT OLD.{column}"
        for column in (
            "repository_id",
            "repository_numeric_id",
            "repository_full_name",
            "base_ref",
            "head_ref",
            "head_sha",
            "expected_old_sha",
            "idempotency_key",
        )
    )
    op.execute(
        "CREATE TRIGGER trg_candidate_publication_identity BEFORE UPDATE ON "
        f"integration_candidate_publications WHEN {publication_identity} BEGIN "
        "SELECT RAISE(ABORT, 'candidate publication identity is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_candidate_publication_state BEFORE UPDATE ON "
        "integration_candidate_publications WHEN CASE NEW.state WHEN 'reserved' THEN 0 "
        "WHEN 'ref_published' THEN 1 WHEN 'pr_reserved' THEN 2 ELSE 3 END < "
        "CASE OLD.state WHEN 'reserved' THEN 0 WHEN 'ref_published' THEN 1 "
        "WHEN 'pr_reserved' THEN 2 ELSE 3 END BEGIN SELECT RAISE(ABORT, "
        "'candidate publication state cannot regress'); END"
    )
    resolution_identity = " OR ".join(
        f"NEW.{column} IS NOT OLD.{column}"
        for column in (
            "batch_id",
            "revision",
            "member_ordinal",
            "operation_id",
            "stage_ordinal",
            "repair_task_id",
            "repair_session_id",
            "repair_session_instance_token",
            "repair_workspace_id",
            "repository_id",
            "branch",
            "fence_owner_id",
            "fence_token",
            "partial_head_sha",
            "source_base_sha",
            "source_head_sha",
            "resolved_head_sha",
            "resolved_tree_sha",
            "repair_commit_shas",
        )
    )
    op.execute(
        "CREATE TRIGGER trg_candidate_resolution_identity BEFORE UPDATE ON "
        f"integration_candidate_resolutions WHEN {resolution_identity} BEGIN "
        "SELECT RAISE(ABORT, 'candidate resolution identity is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_candidate_resolution_push_immutable BEFORE UPDATE ON "
        "integration_candidate_resolutions WHEN OLD.push_evidence IS NOT NULL AND "
        "NEW.push_evidence IS NOT OLD.push_evidence BEGIN SELECT RAISE(ABORT, "
        "'candidate resolution push evidence is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_candidate_resolution_state BEFORE UPDATE ON "
        "integration_candidate_resolutions WHEN CASE NEW.state WHEN 'reserved' THEN 0 "
        "WHEN 'pushed' THEN 1 ELSE 2 END < CASE OLD.state WHEN 'reserved' THEN 0 "
        "WHEN 'pushed' THEN 1 ELSE 2 END BEGIN SELECT RAISE(ABORT, "
        "'candidate resolution state cannot regress'); END"
    )


def upgrade() -> None:
    """Upgrade schema."""
    _guard_names()
    op.create_table(
        "integration_candidate_publications",
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("repository_numeric_id", sa.Integer(), nullable=False),
        sa.Column("repository_full_name", sa.Text(), nullable=False),
        sa.Column("base_ref", sa.Text(), nullable=False),
        sa.Column("head_ref", sa.Text(), nullable=False),
        sa.Column("head_sha", sa.Text(), nullable=False),
        sa.Column("expected_old_sha", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_integration_candidate_publications_revision"),
        sa.CheckConstraint(
            "repository_numeric_id > 0",
            name="ck_integration_candidate_publications_repository_numeric",
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'ref_published', 'pr_reserved', 'pr_published')",
            name="ck_integration_candidate_publications_state",
        ),
        sa.CheckConstraint(
            "(state = 'pr_published' AND pr_number IS NOT NULL AND pr_number > 0 AND pr_url IS NOT NULL) OR (state <> 'pr_published' AND pr_number IS NULL AND pr_url IS NULL)",
            name="ck_integration_candidate_publications_pr_identity",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id", "revision"],
            [
                "integration_candidate_revisions.batch_id",
                "integration_candidate_revisions.revision",
            ],
            name="fk_integration_candidate_publications_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("batch_id", "revision"),
        sa.UniqueConstraint("idempotency_key", name="uq_integration_candidate_publications_key"),
    )
    op.create_table(
        "integration_candidate_resolutions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("member_ordinal", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("stage_ordinal", sa.Integer(), nullable=False),
        sa.Column("repair_task_id", sa.Text(), nullable=False),
        sa.Column("repair_session_id", sa.Text(), nullable=False),
        sa.Column("repair_session_instance_token", sa.Text(), nullable=False),
        sa.Column("repair_workspace_id", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("fence_owner_id", sa.Text(), nullable=False),
        sa.Column("fence_token", sa.Integer(), nullable=False),
        sa.Column("partial_head_sha", sa.Text(), nullable=False),
        sa.Column("source_base_sha", sa.Text(), nullable=False),
        sa.Column("source_head_sha", sa.Text(), nullable=False),
        sa.Column("resolved_head_sha", sa.Text(), nullable=False),
        sa.Column("resolved_tree_sha", sa.Text(), nullable=False),
        sa.Column("repair_commit_shas", sa.JSON(), nullable=False),
        sa.Column("push_evidence", sa.JSON(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_integration_candidate_resolutions_revision"),
        sa.CheckConstraint(
            "member_ordinal >= 0", name="ck_integration_candidate_resolutions_member_ordinal"
        ),
        sa.CheckConstraint(
            "stage_ordinal IN (0, 1)", name="ck_integration_candidate_resolutions_stage"
        ),
        sa.CheckConstraint("fence_token >= 0", name="ck_integration_candidate_resolutions_fence"),
        sa.CheckConstraint(
            "state IN ('reserved', 'pushed', 'accepted')",
            name="ck_integration_candidate_resolutions_state",
        ),
        sa.CheckConstraint(
            "(state = 'reserved' AND push_evidence IS NULL) OR (state IN ('pushed', 'accepted') AND push_evidence IS NOT NULL)",
            name="ck_integration_candidate_resolutions_push",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id", "revision", "member_ordinal"],
            [
                "integration_candidate_member_results.batch_id",
                "integration_candidate_member_results.revision",
                "integration_candidate_member_results.member_ordinal",
            ],
            name="fk_integration_candidate_resolutions_member",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "stage_ordinal"],
            ["integration_repair_stages.operation_id", "integration_repair_stages.ordinal"],
            name="fk_integration_candidate_resolutions_stage",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["repair_task_id"], ["tasks.id"], name="fk_integration_candidate_resolutions_task"
        ),
        sa.ForeignKeyConstraint(
            ["repair_session_id"],
            ["sessions.id"],
            name="fk_integration_candidate_resolutions_session",
        ),
        sa.ForeignKeyConstraint(
            ["repair_workspace_id"],
            ["workspaces.id"],
            name="fk_integration_candidate_resolutions_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id",
            "revision",
            "member_ordinal",
            name="uq_integration_candidate_resolutions_member",
        ),
    )
    _create_guards()


def downgrade() -> None:
    """Downgrade schema."""
    for table in ("integration_candidate_publications", "integration_candidate_resolutions"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(f"cannot downgrade while live {table} rows exist")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER trg_candidate_resolution_monotone ON integration_candidate_resolutions"
        )
        op.execute("DROP FUNCTION integration_candidate_resolution_is_monotone()")
        op.execute(
            "DROP TRIGGER trg_candidate_publication_monotone ON integration_candidate_publications"
        )
        op.execute("DROP FUNCTION integration_candidate_publication_is_monotone()")
    else:
        for trigger in (
            "trg_candidate_resolution_state",
            "trg_candidate_resolution_push_immutable",
            "trg_candidate_resolution_identity",
            "trg_candidate_publication_state",
            "trg_candidate_publication_identity",
        ):
            op.execute(f"DROP TRIGGER {trigger}")
    op.drop_table("integration_candidate_resolutions")
    op.drop_table("integration_candidate_publications")
