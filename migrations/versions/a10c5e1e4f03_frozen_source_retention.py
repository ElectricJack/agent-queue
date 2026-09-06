"""freeze integration source retention and tighten cleanup targets

Revision ID: a10c5e1e4f03
Revises: a10c5e1e4f02
Create Date: 2026-09-06 22:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "a10c5e1e4f03"
down_revision: str | Sequence[str] | None = "a10c5e1e4f02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_RETENTION = (
    "(source_ref IS NULL AND source_ref_retention IS NULL) OR "
    "(source_ref IS NOT NULL AND source_ref LIKE 'refs/heads/%' AND "
    "source_ref_retention IN ('delete', 'retain'))"
)
_TARGET = (
    "(kind = 'source_pr' AND member_ordinal IS NOT NULL AND member_ordinal >= 0 "
    "AND receipt_id IS NOT NULL AND target_pr_number IS NOT NULL "
    "AND target_pr_number > 0 AND target_pr_url IS NOT NULL "
    "AND target_ref IS NULL AND workspace_path IS NULL) OR "
    "(kind = 'audit_pr' AND member_ordinal IS NULL AND receipt_id IS NULL "
    "AND target_pr_number IS NOT NULL AND target_pr_number > 0 "
    "AND target_pr_url IS NOT NULL AND target_ref IS NULL "
    "AND workspace_path IS NULL) OR "
    "(kind = 'remote_ref' AND (member_ordinal IS NULL OR member_ordinal >= 0) "
    "AND receipt_id IS NULL AND target_pr_number IS NULL AND target_pr_url IS NULL "
    "AND target_ref IS NOT NULL AND workspace_path IS NULL) OR "
    "(kind = 'local_ref' AND member_ordinal IS NULL AND receipt_id IS NULL "
    "AND target_pr_number IS NULL AND target_pr_url IS NULL "
    "AND target_ref IS NOT NULL AND workspace_path IS NULL) OR "
    "(kind = 'worktree' AND member_ordinal IS NULL AND receipt_id IS NULL "
    "AND target_pr_number IS NULL AND target_pr_url IS NULL AND target_ref IS NULL "
    "AND workspace_path IS NOT NULL)"
)
_OLD_TARGET = _TARGET.replace(
    "kind = 'remote_ref' AND (member_ordinal IS NULL OR member_ordinal >= 0)",
    "kind = 'remote_ref' AND member_ordinal IS NULL",
).replace("target_pr_number IS NOT NULL AND ", "")

_CLEANUP_IMMUTABLE = (
    "batch_id", "kind", "identity", "domain_key", "project_id", "repository_id",
    "repository_numeric_id", "repository_full_name", "revision", "member_ordinal",
    "receipt_id", "target_ref", "target_pr_number", "target_pr_url", "workspace_path",
    "expected_sha", "created_at",
)


def _drop_sqlite_guards() -> None:
    for event in ("insert", "update", "delete"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_integration_members_{event}")
    op.execute("DROP TRIGGER IF EXISTS trg_integration_cleanup_item_guard")
    op.execute("DROP TRIGGER IF EXISTS trg_integration_cleanup_irreversible_guard")
    op.execute("DROP TRIGGER IF EXISTS trg_integration_batch_empty_dependencies")


def _create_sqlite_guards() -> None:
    op.execute(
        "CREATE TRIGGER trg_integration_members_insert BEFORE INSERT ON "
        "integration_batch_members WHEN NOT EXISTS (SELECT 1 FROM integration_batches "
        "WHERE id = NEW.batch_id AND lifecycle = 'sealing') BEGIN SELECT RAISE(ABORT, "
        "'sealed integration batch membership is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_integration_members_update BEFORE UPDATE ON "
        "integration_batch_members WHEN NOT EXISTS (SELECT 1 FROM integration_batches "
        "WHERE id = OLD.batch_id AND lifecycle = 'sealing') OR NOT EXISTS (SELECT 1 FROM "
        "integration_batches WHERE id = NEW.batch_id AND lifecycle = 'sealing') BEGIN "
        "SELECT RAISE(ABORT, 'sealed integration batch membership is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_integration_members_delete BEFORE DELETE ON "
        "integration_batch_members WHEN NOT EXISTS (SELECT 1 FROM integration_batches "
        "WHERE id = OLD.batch_id AND lifecycle = 'sealing') BEGIN SELECT RAISE(ABORT, "
        "'sealed integration batch membership is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_integration_batch_empty_dependencies BEFORE UPDATE ON "
        "integration_batches WHEN NEW.lifecycle = 'empty' AND (EXISTS (SELECT 1 FROM "
        "integration_batch_members WHERE batch_id = NEW.id) OR EXISTS (SELECT 1 FROM "
        "integration_repair_operations WHERE batch_id = NEW.id) OR EXISTS (SELECT 1 FROM "
        "project_integration_leases WHERE batch_id = NEW.id)) BEGIN SELECT RAISE(ABORT, "
        "'empty integration batch cannot retain members, repair operations, or leases'); "
        "END"
    )
    op.execute(
        "CREATE TRIGGER trg_integration_cleanup_item_guard BEFORE UPDATE ON "
        "integration_cleanup_items WHEN "
        + " OR ".join(f"NEW.{name} IS NOT OLD.{name}" for name in _CLEANUP_IMMUTABLE)
        + " OR OLD.state IN ('complete', 'conflict', 'failed') "
        "BEGIN SELECT RAISE(ABORT, 'integration cleanup identity is immutable'); END"
    )
    op.execute(
        """CREATE TRIGGER trg_integration_cleanup_irreversible_guard
        BEFORE UPDATE ON integration_cleanup_items
        WHEN
          (OLD.irreversible_nonce IS NOT NULL AND
           (NEW.irreversible_nonce IS NOT OLD.irreversible_nonce OR
            NEW.irreversible_prewrite_at IS NOT OLD.irreversible_prewrite_at))
          OR ((NEW.irreversible_nonce IS NULL) !=
              (NEW.irreversible_prewrite_at IS NULL))
          OR (OLD.irreversible_prewrite_at IS NULL AND
              NEW.irreversible_prewrite_at IS NOT NULL AND
              NEW.irreversible_nonce IS NOT OLD.execution_nonce)
        BEGIN SELECT RAISE(ABORT, 'cleanup irreversible prewrite is immutable'); END"""
    )


def _replace_sqlite_constraints(*, target: str, add_source: bool) -> None:
    _drop_sqlite_guards()
    with op.batch_alter_table(
        "integration_batch_members", recreate="always"
    ) as batch_op:
        if add_source:
            batch_op.add_column(sa.Column("source_ref", sa.Text(), nullable=True))
            batch_op.add_column(
                sa.Column("source_ref_retention", sa.Text(), nullable=True)
            )
            batch_op.create_check_constraint(
                "ck_integration_batch_members_source_retention", _SOURCE_RETENTION
            )
        else:
            batch_op.drop_constraint(
                "ck_integration_batch_members_source_retention", type_="check"
            )
            batch_op.drop_column("source_ref_retention")
            batch_op.drop_column("source_ref")
    with op.batch_alter_table(
        "integration_cleanup_items", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("ck_integration_cleanup_items_target", type_="check")
        batch_op.create_check_constraint("ck_integration_cleanup_items_target", target)
    _create_sqlite_guards()


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.add_column(
            "integration_batch_members", sa.Column("source_ref", sa.Text(), nullable=True)
        )
        op.add_column(
            "integration_batch_members",
            sa.Column("source_ref_retention", sa.Text(), nullable=True),
        )
        op.create_check_constraint(
            "ck_integration_batch_members_source_retention",
            "integration_batch_members",
            _SOURCE_RETENTION,
        )
        op.drop_constraint(
            "ck_integration_cleanup_items_target",
            "integration_cleanup_items",
            type_="check",
        )
        op.create_check_constraint(
            "ck_integration_cleanup_items_target",
            "integration_cleanup_items",
            _TARGET,
        )
        return
    _replace_sqlite_constraints(target=_TARGET, add_source=True)


def downgrade() -> None:
    bind = op.get_bind()
    source_item = bind.execute(
        sa.text(
            "SELECT batch_id || ':' || identity FROM integration_cleanup_items "
            "WHERE kind = 'remote_ref' AND member_ordinal IS NOT NULL LIMIT 1"
        )
    ).scalar_one_or_none()
    frozen_member = bind.execute(
        sa.text(
            "SELECT batch_id || ':' || ordinal FROM integration_batch_members "
            "WHERE source_ref IS NOT NULL OR source_ref_retention IS NOT NULL LIMIT 1"
        )
    ).scalar_one_or_none()
    if source_item is not None or frozen_member is not None:
        identity = source_item or frozen_member
        raise RuntimeError(
            f"drain frozen source cleanup identity {identity} before downgrade"
        )
    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "ck_integration_cleanup_items_target",
            "integration_cleanup_items",
            type_="check",
        )
        op.create_check_constraint(
            "ck_integration_cleanup_items_target",
            "integration_cleanup_items",
            _OLD_TARGET,
        )
        op.drop_constraint(
            "ck_integration_batch_members_source_retention",
            "integration_batch_members",
            type_="check",
        )
        op.drop_column("integration_batch_members", "source_ref_retention")
        op.drop_column("integration_batch_members", "source_ref")
        return
    _replace_sqlite_constraints(target=_OLD_TARGET, add_source=False)
