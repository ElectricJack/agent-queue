"""hierarchical integration state

Revision ID: 3f30b34c7e7c
Revises: e6a1b2c3d4f5
Create Date: 2026-09-04 19:53:59.203608

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f30b34c7e7c'
down_revision: Union[str, Sequence[str], None] = 'e6a1b2c3d4f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_immutability_guards() -> None:
    """Enforce sealed manifests and monotone repair counters in both dialects."""
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION integration_member_is_mutable() RETURNS trigger AS $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM integration_batches
                    WHERE id = COALESCE(NEW.batch_id, OLD.batch_id) AND lifecycle = 'sealing'
                ) THEN
                    RAISE EXCEPTION 'sealed integration batch membership is immutable';
                END IF;
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for event in ("INSERT", "UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_integration_members_{event.lower()} "
                f"BEFORE {event} ON integration_batch_members FOR EACH ROW "
                "EXECUTE FUNCTION integration_member_is_mutable()"
            )
        op.execute(
            """
            CREATE FUNCTION integration_repair_attempts_monotone() RETURNS trigger AS $$
            BEGIN
                IF NEW.attempts < OLD.attempts THEN
                    RAISE EXCEPTION 'integration repair attempts cannot decrease';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            "CREATE TRIGGER trg_integration_repair_attempts_monotone "
            "BEFORE UPDATE ON integration_repair_stages FOR EACH ROW "
            "EXECUTE FUNCTION integration_repair_attempts_monotone()"
        )
        op.execute(
            """
            CREATE FUNCTION task_branch_origin_materialized_immutable() RETURNS trigger AS $$
            BEGIN
                IF OLD.materialized THEN
                    RAISE EXCEPTION 'materialized task branch origin is immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            "CREATE TRIGGER trg_task_branch_origins_materialized_immutable "
            "BEFORE UPDATE ON task_branch_origins FOR EACH ROW "
            "EXECUTE FUNCTION task_branch_origin_materialized_immutable()"
        )
        return

    for event in ("INSERT", "UPDATE", "DELETE"):
        reference = "NEW.batch_id" if event != "DELETE" else "OLD.batch_id"
        op.execute(
            f"""
            CREATE TRIGGER trg_integration_members_{event.lower()}
            BEFORE {event} ON integration_batch_members
            WHEN NOT EXISTS (
                SELECT 1 FROM integration_batches
                WHERE id = {reference} AND lifecycle = 'sealing'
            )
            BEGIN
                SELECT RAISE(ABORT, 'sealed integration batch membership is immutable');
            END
            """
        )
    op.execute(
        """
        CREATE TRIGGER trg_integration_repair_attempts_monotone
        BEFORE UPDATE OF attempts ON integration_repair_stages
        WHEN NEW.attempts < OLD.attempts
        BEGIN
            SELECT RAISE(ABORT, 'integration repair attempts cannot decrease');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_task_branch_origins_materialized_immutable
        BEFORE UPDATE ON task_branch_origins
        WHEN OLD.materialized = 1
        BEGIN
            SELECT RAISE(ABORT, 'materialized task branch origin is immutable');
        END
        """
    )


def _drop_immutability_guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for event in ("insert", "update", "delete"):
            op.execute(f"DROP TRIGGER trg_integration_members_{event} ON integration_batch_members")
        op.execute("DROP TRIGGER trg_integration_repair_attempts_monotone ON integration_repair_stages")
        op.execute(
            "DROP TRIGGER trg_task_branch_origins_materialized_immutable ON task_branch_origins"
        )
        op.execute("DROP FUNCTION integration_member_is_mutable()")
        op.execute("DROP FUNCTION integration_repair_attempts_monotone()")
        op.execute("DROP FUNCTION task_branch_origin_materialized_immutable()")
        return
    for event in ("insert", "update", "delete"):
        op.execute(f"DROP TRIGGER trg_integration_members_{event}")
    op.execute("DROP TRIGGER trg_integration_repair_attempts_monotone")
    op.execute("DROP TRIGGER trg_task_branch_origins_materialized_immutable")


def upgrade() -> None:
    """Upgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('integration_batch_members',
    sa.Column('batch_id', sa.Text(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Text(), nullable=False),
    sa.Column('pr_url', sa.Text(), nullable=True),
    sa.Column('repository_id', sa.Text(), nullable=False),
    sa.Column('source_base_sha', sa.Text(), nullable=False),
    sa.Column('reviewed_head_sha', sa.Text(), nullable=False),
    sa.Column('reviewed_tree_sha', sa.Text(), nullable=False),
    sa.Column('review_evidence', sa.JSON(), nullable=False),
    sa.CheckConstraint('ordinal >= 0', name='ck_integration_batch_members_ordinal'),
    sa.PrimaryKeyConstraint('batch_id', 'ordinal'),
    sa.UniqueConstraint('batch_id', 'task_id', name='uq_integration_batch_members_task')
    )
    op.create_table('integration_batches',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('project_id', sa.Text(), nullable=False),
    sa.Column('repository_id', sa.Text(), nullable=False),
    sa.Column('trigger', sa.Text(), nullable=True),
    sa.Column('source_manifest_digest', sa.Text(), nullable=False),
    sa.Column('base_sha', sa.Text(), nullable=True),
    sa.Column('lifecycle', sa.Text(), nullable=False),
    sa.Column('current_revision', sa.Integer(), server_default='0', nullable=False),
    sa.Column('integration_branch', sa.Text(), nullable=True),
    sa.Column('pr_url', sa.Text(), nullable=True),
    sa.Column('repair_stage_ordinal', sa.Integer(), nullable=True),
    sa.Column('tested_candidate_sha', sa.Text(), nullable=True),
    sa.Column('ci_evidence_id', sa.Text(), nullable=True),
    sa.Column('final_main_sha', sa.Text(), nullable=True),
    sa.Column('human_abort_reason', sa.Text(), nullable=True),
    sa.Column('policy_snapshot', sa.JSON(), nullable=False),
    sa.Column('artifact_snapshot', sa.JSON(), nullable=False),
    sa.Column('cleanup_state', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.CheckConstraint("lifecycle IN ('sealing', 'sealed', 'building', 'testing', 'repairing', 'human_blocked', 'promoting', 'cleanup_pending', 'promoted', 'aborted', 'failed')", name='ck_integration_batches_lifecycle'),
    sa.CheckConstraint('current_revision >= 0', name='ck_integration_batches_revision'),
    sa.CheckConstraint('repair_stage_ordinal IS NULL OR repair_stage_ordinal >= 0', name='ck_integration_batches_repair_stage'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('integration_batches', schema=None) as batch_op:
        batch_op.create_index('uq_integration_batches_active_project', ['project_id'], unique=True, sqlite_where=sa.text("lifecycle IN ('sealing', 'sealed', 'building', 'testing', 'repairing', 'human_blocked', 'promoting', 'cleanup_pending')"), postgresql_where=sa.text("lifecycle IN ('sealing', 'sealed', 'building', 'testing', 'repairing', 'human_blocked', 'promoting', 'cleanup_pending')"))

    op.create_table('integration_branch_owners',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('repository_id', sa.Text(), nullable=False),
    sa.Column('ref', sa.Text(), nullable=False),
    sa.Column('owner_id', sa.Text(), nullable=False),
    sa.Column('owner_role', sa.Text(), nullable=False),
    sa.Column('fence_token', sa.Integer(), nullable=False),
    sa.Column('handoff_state', sa.Text(), server_default='reserved', nullable=False),
    sa.Column('session_id', sa.Text(), nullable=True),
    sa.Column('workspace_id', sa.Text(), nullable=True),
    sa.Column('confirmed_workspace_id', sa.Text(), nullable=True),
    sa.Column('expires_at', sa.Float(), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.CheckConstraint("handoff_state IN ('reserved', 'attached', 'handoff_pending', 'released')", name='ck_integration_branch_owners_handoff_state'),
    sa.CheckConstraint('fence_token >= 0', name='ck_integration_branch_owners_fence'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('repository_id', 'ref', name='uq_integration_branch_owners_ref')
    )
    op.create_table('integration_candidate_revisions',
    sa.Column('batch_id', sa.Text(), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('construction_base_sha', sa.Text(), nullable=False),
    sa.Column('next_member_ordinal', sa.Integer(), server_default='0', nullable=False),
    sa.Column('repair_parent_revision', sa.Integer(), nullable=True),
    sa.Column('head_sha', sa.Text(), nullable=True),
    sa.Column('ci_evidence_id', sa.Text(), nullable=True),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.CheckConstraint("state IN ('constructing', 'built', 'testing', 'green', 'red', 'superseded', 'promoted')", name='ck_integration_candidate_revisions_state'),
    sa.CheckConstraint('next_member_ordinal >= 0', name='ck_integration_candidate_revisions_next_member'),
    sa.CheckConstraint('repair_parent_revision IS NULL OR repair_parent_revision >= 0', name='ck_integration_candidate_revisions_repair_parent'),
    sa.CheckConstraint('revision >= 0', name='ck_integration_candidate_revisions_revision'),
    sa.PrimaryKeyConstraint('batch_id', 'revision')
    )
    op.create_table('integration_check_evidence',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('operation_id', sa.Text(), nullable=True),
    sa.Column('batch_id', sa.Text(), nullable=True),
    sa.Column('candidate_revision', sa.Integer(), nullable=True),
    sa.Column('parent_task_id', sa.Text(), nullable=True),
    sa.Column('parent_generation', sa.Integer(), nullable=True),
    sa.Column('parent_head_sha', sa.Text(), nullable=True),
    sa.Column('producer_id', sa.Text(), nullable=False),
    sa.Column('workflow_id', sa.Text(), nullable=False),
    sa.Column('run_id', sa.Text(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('required_check_version', sa.Text(), nullable=False),
    sa.Column('checks', sa.JSON(), nullable=False),
    sa.Column('conclusion', sa.Text(), nullable=False),
    sa.Column('classification', sa.Text(), nullable=False),
    sa.Column('observed_at', sa.Float(), nullable=False),
    sa.CheckConstraint("conclusion IN ('success', 'failure', 'pending', 'cancelled', 'inconclusive')", name='ck_integration_check_evidence_conclusion'),
    sa.CheckConstraint('(batch_id IS NOT NULL AND candidate_revision IS NOT NULL AND parent_task_id IS NULL AND parent_generation IS NULL AND parent_head_sha IS NULL) OR (batch_id IS NULL AND candidate_revision IS NULL AND parent_task_id IS NOT NULL AND parent_generation IS NOT NULL AND parent_head_sha IS NOT NULL)', name='ck_integration_check_evidence_subject'),
    sa.CheckConstraint('attempt >= 0', name='ck_integration_check_evidence_attempt'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('producer_id', 'run_id', 'attempt', 'required_check_version', name='uq_integration_check_evidence_producer_run_attempt_checks')
    )
    op.create_table('integration_outbox',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('dedup_key', sa.Text(), nullable=False),
    sa.Column('project_id', sa.Text(), nullable=False),
    sa.Column('event_type', sa.Text(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('available_at', sa.Float(), nullable=False),
    sa.Column('delivered_at', sa.Float(), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.CheckConstraint('attempts >= 0', name='ck_integration_outbox_attempts'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dedup_key', name='uq_integration_outbox_dedup_key')
    )
    with op.batch_alter_table('integration_outbox', schema=None) as batch_op:
        batch_op.create_index('idx_integration_outbox_pending_available', ['available_at'], unique=False, sqlite_where=sa.text('delivered_at IS NULL'), postgresql_where=sa.text('delivered_at IS NULL'))

    op.create_table('integration_promotion_intents',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('domain_key', sa.Text(), nullable=False),
    sa.Column('receipt_id', sa.Text(), nullable=False),
    sa.Column('source_task_id', sa.Text(), nullable=True),
    sa.Column('source_head', sa.Text(), nullable=False),
    sa.Column('source_base', sa.Text(), nullable=False),
    sa.Column('repository_id', sa.Text(), nullable=False),
    sa.Column('target_branch', sa.Text(), nullable=False),
    sa.Column('expected_target', sa.Text(), nullable=False),
    sa.Column('prepared_sha', sa.Text(), nullable=True),
    sa.Column('recovery_ref', sa.Text(), nullable=True),
    sa.Column('fence_owner_id', sa.Text(), nullable=False),
    sa.Column('fence_token', sa.Integer(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('remote_evidence', sa.JSON(), nullable=True),
    sa.Column('committed_at', sa.Float(), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.CheckConstraint("state IN ('reserved', 'prepared', 'pushed', 'reconciled', 'committed', 'conflict')", name='ck_integration_promotion_intents_state'),
    sa.CheckConstraint("(state <> 'committed' OR (committed_at IS NOT NULL AND remote_evidence IS NOT NULL)) AND (committed_at IS NULL OR remote_evidence IS NOT NULL)", name='ck_integration_promotion_intents_committed_evidence'),
    sa.CheckConstraint('fence_token >= 0', name='ck_integration_promotion_intents_fence'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('domain_key', name='uq_integration_promotion_intents_domain_key')
    )
    op.create_table('integration_repair_operations',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('target_kind', sa.Text(), nullable=False),
    sa.Column('batch_id', sa.Text(), nullable=True),
    sa.Column('parent_task_id', sa.Text(), nullable=True),
    sa.Column('episode_id', sa.Text(), nullable=False),
    sa.Column('active_stage', sa.Integer(), server_default='0', nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('policy_snapshot', sa.JSON(), nullable=False),
    sa.Column('artifact_snapshot', sa.JSON(), nullable=False),
    sa.Column('required_check_version', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.CheckConstraint("(target_kind = 'batch' AND batch_id IS NOT NULL AND parent_task_id IS NULL) OR (target_kind = 'parent' AND parent_task_id IS NOT NULL AND batch_id IS NULL)", name='ck_integration_repair_operations_target'),
    sa.CheckConstraint("state IN ('active', 'escalated', 'human_required', 'completed', 'cancelled')", name='ck_integration_repair_operations_state'),
    sa.CheckConstraint('active_stage >= 0', name='ck_integration_repair_operations_active_stage'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('integration_repair_operations', schema=None) as batch_op:
        batch_op.create_index('uq_integration_repair_operations_active_batch', ['batch_id'], unique=True, sqlite_where=sa.text("batch_id IS NOT NULL AND state IN ('active', 'escalated', 'human_required')"), postgresql_where=sa.text("batch_id IS NOT NULL AND state IN ('active', 'escalated', 'human_required')"))
        batch_op.create_index('uq_integration_repair_operations_active_parent', ['parent_task_id', 'episode_id'], unique=True, sqlite_where=sa.text("parent_task_id IS NOT NULL AND state IN ('active', 'escalated', 'human_required')"), postgresql_where=sa.text("parent_task_id IS NOT NULL AND state IN ('active', 'escalated', 'human_required')"))

    op.create_table('integration_repair_stages',
    sa.Column('operation_id', sa.Text(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('policy', sa.JSON(), nullable=False),
    sa.Column('intelligence_class', sa.Text(), nullable=False),
    sa.Column('profile_id', sa.Text(), nullable=True),
    sa.Column('repair_task_id', sa.Text(), nullable=True),
    sa.Column('starting_sha', sa.Text(), nullable=False),
    sa.Column('started_at', sa.Float(), nullable=True),
    sa.Column('deadline_at', sa.Float(), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('dossier', sa.JSON(), nullable=True),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('completed_at', sa.Float(), nullable=True),
    sa.CheckConstraint("state IN ('pending', 'active', 'passed', 'failed', 'expired', 'cancelled')", name='ck_integration_repair_stages_state'),
    sa.CheckConstraint('attempts >= 0', name='ck_integration_repair_stages_attempts'),
    sa.CheckConstraint('ordinal IN (0, 1)', name='ck_integration_repair_stages_ordinal'),
    sa.PrimaryKeyConstraint('operation_id', 'ordinal')
    )
    op.create_table('project_integration_leases',
    sa.Column('project_id', sa.Text(), nullable=False),
    sa.Column('repository_id', sa.Text(), nullable=False),
    sa.Column('batch_id', sa.Text(), nullable=False),
    sa.Column('owner_id', sa.Text(), nullable=False),
    sa.Column('fence_token', sa.Integer(), nullable=False),
    sa.Column('heartbeat_at', sa.Float(), nullable=False),
    sa.Column('expires_at', sa.Float(), nullable=False),
    sa.CheckConstraint('expires_at >= heartbeat_at', name='ck_project_integration_leases_expiry'),
    sa.CheckConstraint('fence_token >= 0', name='ck_project_integration_leases_fence'),
    sa.PrimaryKeyConstraint('project_id')
    )
    op.create_table('project_integration_schedules',
    sa.Column('project_id', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.Column('interval_seconds', sa.Integer(), nullable=False),
    sa.Column('next_due_at', sa.Float(), nullable=False),
    sa.Column('last_observed_window', sa.Float(), nullable=True),
    sa.Column('request_sequence', sa.Integer(), server_default='0', nullable=False),
    sa.Column('outstanding_request_id', sa.Text(), nullable=True),
    sa.Column('outstanding_trigger', sa.Text(), nullable=True),
    sa.Column('outstanding_requested_at', sa.Float(), nullable=True),
    sa.Column('last_completed_sweep_at', sa.Float(), nullable=True),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.CheckConstraint('(outstanding_request_id IS NULL AND outstanding_trigger IS NULL AND outstanding_requested_at IS NULL) OR (outstanding_request_id IS NOT NULL AND outstanding_trigger IS NOT NULL AND outstanding_requested_at IS NOT NULL)', name='ck_project_integration_schedules_outstanding_request'),
    sa.CheckConstraint('interval_seconds > 0', name='ck_project_integration_schedules_interval'),
    sa.CheckConstraint('request_sequence >= 0', name='ck_project_integration_schedules_sequence'),
    sa.PrimaryKeyConstraint('project_id')
    )
    op.create_table('task_branch_origins',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('task_id', sa.Text(), nullable=False),
    sa.Column('repository_id', sa.Text(), nullable=False),
    sa.Column('parent_task_id', sa.Text(), nullable=True),
    sa.Column('parent_repository_id', sa.Text(), nullable=True),
    sa.Column('parent_ref', sa.Text(), nullable=True),
    sa.Column('base_sha', sa.Text(), nullable=False),
    sa.Column('creation_generation', sa.Integer(), nullable=False),
    sa.Column('reserved', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.Column('materialized', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.Column('retired_at', sa.Float(), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('materialized_at', sa.Float(), nullable=True),
    sa.CheckConstraint('creation_generation >= 0', name='ck_task_branch_origins_generation'),
    sa.CheckConstraint('materialized = false OR reserved = true', name='ck_task_branch_origins_materialized_reserved'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('task_branch_origins', schema=None) as batch_op:
        batch_op.create_index('uq_task_branch_origins_live_task_repo', ['task_id', 'repository_id'], unique=True, sqlite_where=sa.text('retired_at IS NULL'), postgresql_where=sa.text('retired_at IS NULL'))

    op.create_table('task_delivery_receipts',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('domain_key', sa.Text(), nullable=False),
    sa.Column('source_task_id', sa.Text(), nullable=True),
    sa.Column('target_task_id', sa.Text(), nullable=True),
    sa.Column('repository_id', sa.Text(), nullable=False),
    sa.Column('target_branch', sa.Text(), nullable=False),
    sa.Column('workspace_kind', sa.Text(), nullable=True),
    sa.Column('source_pr', sa.Text(), nullable=True),
    sa.Column('reviewed_head_sha', sa.Text(), nullable=True),
    sa.Column('reviewed_tree_sha', sa.Text(), nullable=True),
    sa.Column('before_sha', sa.Text(), nullable=True),
    sa.Column('squash_sha', sa.Text(), nullable=True),
    sa.Column('after_sha', sa.Text(), nullable=True),
    sa.Column('review_evidence', sa.JSON(), nullable=True),
    sa.Column('verification_evidence', sa.JSON(), nullable=True),
    sa.Column('resolution_evidence', sa.JSON(), nullable=True),
    sa.Column('batch_id', sa.Text(), nullable=True),
    sa.Column('member_ordinal', sa.Integer(), nullable=True),
    sa.Column('candidate_revision', sa.Integer(), nullable=True),
    sa.Column('disposition', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.CheckConstraint("disposition = 'code' OR resolution_evidence IS NOT NULL", name='ck_task_delivery_receipts_disposition_evidence'),
    sa.CheckConstraint("disposition IN ('code', 'noop', 'ineligible', 'skipped', 'failed')", name='ck_task_delivery_receipts_disposition'),
    sa.CheckConstraint('candidate_revision IS NULL OR candidate_revision >= 0', name='ck_task_delivery_receipts_candidate_revision'),
    sa.CheckConstraint('member_ordinal IS NULL OR member_ordinal >= 0', name='ck_task_delivery_receipts_member_ordinal'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('domain_key', name='uq_task_delivery_receipts_domain_key')
    )
    with op.batch_alter_table('task_delivery_receipts', schema=None) as batch_op:
        batch_op.create_index('idx_task_delivery_receipts_source', ['source_task_id', 'repository_id'], unique=False)

    op.create_table('task_integration_checkpoints',
    sa.Column('task_id', sa.Text(), nullable=False),
    sa.Column('repository_id', sa.Text(), nullable=False),
    sa.Column('branch', sa.Text(), nullable=False),
    sa.Column('generation', sa.Integer(), server_default='0', nullable=False),
    sa.Column('checkpoint_sha', sa.Text(), nullable=True),
    sa.Column('verified_sha', sa.Text(), nullable=True),
    sa.Column('verified_generation', sa.Integer(), nullable=True),
    sa.Column('state', sa.Text(), server_default='working', nullable=False),
    sa.Column('version', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_transition_id', sa.Text(), nullable=True),
    sa.Column('playbook_activation_id', sa.Text(), nullable=True),
    sa.Column('branch_owner_id', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.CheckConstraint("state IN ('working', 'awaiting_children', 'integration_ready', 'verifying')", name='ck_task_integration_checkpoints_state'),
    sa.CheckConstraint('generation >= 0', name='ck_task_integration_checkpoints_generation'),
    sa.CheckConstraint('verified_generation IS NULL OR verified_generation >= 0', name='ck_task_integration_checkpoints_verified_generation'),
    sa.CheckConstraint('version >= 0', name='ck_task_integration_checkpoints_version'),
    sa.PrimaryKeyConstraint('task_id')
    )
    _create_immutability_guards()

    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    _drop_immutability_guards()

    op.drop_table('task_integration_checkpoints')
    with op.batch_alter_table('task_delivery_receipts', schema=None) as batch_op:
        batch_op.drop_index('idx_task_delivery_receipts_source')

    op.drop_table('task_delivery_receipts')
    with op.batch_alter_table('task_branch_origins', schema=None) as batch_op:
        batch_op.drop_index('uq_task_branch_origins_live_task_repo', sqlite_where=sa.text('retired_at IS NULL'), postgresql_where=sa.text('retired_at IS NULL'))

    op.drop_table('task_branch_origins')
    op.drop_table('project_integration_schedules')
    op.drop_table('project_integration_leases')
    op.drop_table('integration_repair_stages')
    with op.batch_alter_table('integration_repair_operations', schema=None) as batch_op:
        batch_op.drop_index('uq_integration_repair_operations_active_parent', sqlite_where=sa.text("parent_task_id IS NOT NULL AND state IN ('active', 'escalated', 'human_required')"), postgresql_where=sa.text("parent_task_id IS NOT NULL AND state IN ('active', 'escalated', 'human_required')"))
        batch_op.drop_index('uq_integration_repair_operations_active_batch', sqlite_where=sa.text("batch_id IS NOT NULL AND state IN ('active', 'escalated', 'human_required')"), postgresql_where=sa.text("batch_id IS NOT NULL AND state IN ('active', 'escalated', 'human_required')"))

    op.drop_table('integration_repair_operations')
    op.drop_table('integration_promotion_intents')
    with op.batch_alter_table('integration_outbox', schema=None) as batch_op:
        batch_op.drop_index('idx_integration_outbox_pending_available', sqlite_where=sa.text('delivered_at IS NULL'), postgresql_where=sa.text('delivered_at IS NULL'))

    op.drop_table('integration_outbox')
    op.drop_table('integration_check_evidence')
    op.drop_table('integration_candidate_revisions')
    op.drop_table('integration_branch_owners')
    with op.batch_alter_table('integration_batches', schema=None) as batch_op:
        batch_op.drop_index('uq_integration_batches_active_project', sqlite_where=sa.text("lifecycle IN ('sealing', 'sealed', 'building', 'testing', 'repairing', 'human_blocked', 'promoting', 'cleanup_pending')"), postgresql_where=sa.text("lifecycle IN ('sealing', 'sealed', 'building', 'testing', 'repairing', 'human_blocked', 'promoting', 'cleanup_pending')"))

    op.drop_table('integration_batches')
    op.drop_table('integration_batch_members')
    # ### end Alembic commands ###
