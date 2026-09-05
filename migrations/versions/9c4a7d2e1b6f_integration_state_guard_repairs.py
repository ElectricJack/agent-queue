"""Repair integration-state provenance guards and candidate member FKs.

Revision ID: 9c4a7d2e1b6f
Revises: 3f30b34c7e7c
"""

from alembic import op
import sqlalchemy as sa


revision = "9c4a7d2e1b6f"
down_revision = "3f30b34c7e7c"
branch_labels = None
depends_on = None


def _guards() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER trg_integration_prepared_identity_immutable ON integration_promotion_intents")
        op.execute("DROP FUNCTION integration_prepared_identity_immutable()")
        op.execute("""CREATE FUNCTION integration_prepared_identity_immutable() RETURNS trigger AS $$
        BEGIN
          IF OLD.prepared_sha IS NOT NULL AND (NEW.domain_key IS DISTINCT FROM OLD.domain_key OR NEW.receipt_id IS DISTINCT FROM OLD.receipt_id OR NEW.source_task_id IS DISTINCT FROM OLD.source_task_id OR NEW.source_head IS DISTINCT FROM OLD.source_head OR NEW.source_base IS DISTINCT FROM OLD.source_base OR NEW.repository_id IS DISTINCT FROM OLD.repository_id OR NEW.target_branch IS DISTINCT FROM OLD.target_branch OR NEW.expected_target IS DISTINCT FROM OLD.expected_target OR NEW.prepared_sha IS DISTINCT FROM OLD.prepared_sha OR NEW.fence_owner_id IS DISTINCT FROM OLD.fence_owner_id OR NEW.fence_token IS DISTINCT FROM OLD.fence_token OR NEW.recovery_ref IS DISTINCT FROM OLD.recovery_ref) THEN RAISE EXCEPTION 'prepared integration identity is immutable'; END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
        op.execute("CREATE TRIGGER trg_integration_prepared_identity_immutable BEFORE UPDATE ON integration_promotion_intents FOR EACH ROW EXECUTE FUNCTION integration_prepared_identity_immutable()")
        for function, table, condition, message in (
            ("integration_candidate_progress_monotone", "integration_candidate_revisions", "NEW.next_member_ordinal < OLD.next_member_ordinal", "integration candidate progress cannot decrease"),
            ("integration_repair_operation_stage_monotone", "integration_repair_operations", "NEW.active_stage < OLD.active_stage", "integration repair operation stage cannot decrease"),
        ):
            op.execute(f"CREATE FUNCTION {function}() RETURNS trigger AS $$ BEGIN IF {condition} THEN RAISE EXCEPTION '{message}'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql")
            op.execute(f"CREATE TRIGGER trg_{function} BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION {function}()")
        return
    op.execute("DROP TRIGGER trg_integration_prepared_identity_immutable")
    op.execute("CREATE TRIGGER trg_integration_schedule_sequence_monotone BEFORE UPDATE ON project_integration_schedules WHEN NEW.request_sequence < OLD.request_sequence BEGIN SELECT RAISE(ABORT, 'integration schedule request sequence cannot decrease'); END")
    op.execute("""CREATE TRIGGER trg_integration_prepared_identity_immutable BEFORE UPDATE ON integration_promotion_intents WHEN OLD.prepared_sha IS NOT NULL AND (NEW.domain_key IS NOT OLD.domain_key OR NEW.receipt_id IS NOT OLD.receipt_id OR NEW.source_task_id IS NOT OLD.source_task_id OR NEW.source_head IS NOT OLD.source_head OR NEW.source_base IS NOT OLD.source_base OR NEW.repository_id IS NOT OLD.repository_id OR NEW.target_branch IS NOT OLD.target_branch OR NEW.expected_target IS NOT OLD.expected_target OR NEW.prepared_sha IS NOT OLD.prepared_sha OR NEW.fence_owner_id IS NOT OLD.fence_owner_id OR NEW.fence_token IS NOT OLD.fence_token OR NEW.recovery_ref IS NOT OLD.recovery_ref) BEGIN SELECT RAISE(ABORT, 'prepared integration identity is immutable'); END""")
    op.execute("CREATE TRIGGER trg_integration_candidate_progress_monotone BEFORE UPDATE ON integration_candidate_revisions WHEN NEW.next_member_ordinal < OLD.next_member_ordinal BEGIN SELECT RAISE(ABORT, 'integration candidate progress cannot decrease'); END")
    op.execute("CREATE TRIGGER trg_integration_repair_operation_stage_monotone BEFORE UPDATE ON integration_repair_operations WHEN NEW.active_stage < OLD.active_stage BEGIN SELECT RAISE(ABORT, 'integration repair operation stage cannot decrease'); END")


def upgrade() -> None:
    with op.batch_alter_table("project_integration_schedules") as batch:
        batch.alter_column("enabled", existing_type=sa.Boolean(), server_default=sa.false())
    with op.batch_alter_table("integration_candidate_member_results") as batch:
        batch.create_foreign_key("fk_integration_candidate_member_results_revision", "integration_candidate_revisions", ["batch_id", "revision"], ["batch_id", "revision"])
        batch.create_foreign_key("fk_integration_candidate_member_results_member", "integration_batch_members", ["batch_id", "member_ordinal"], ["batch_id", "ordinal"])
    _guards()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for function, table in (("integration_candidate_progress_monotone", "integration_candidate_revisions"), ("integration_repair_operation_stage_monotone", "integration_repair_operations")):
            op.execute(f"DROP TRIGGER trg_{function} ON {table}")
            op.execute(f"DROP FUNCTION {function}()")
    else:
        op.execute("DROP TRIGGER trg_integration_candidate_progress_monotone")
        op.execute("DROP TRIGGER trg_integration_repair_operation_stage_monotone")
    with op.batch_alter_table("integration_candidate_member_results") as batch:
        batch.drop_constraint("fk_integration_candidate_member_results_member", type_="foreignkey")
        batch.drop_constraint("fk_integration_candidate_member_results_revision", type_="foreignkey")
