"""PostgreSQL guards preserved from the pre-squash migration graph.

Table metadata cannot represent procedural triggers. These are the final
definitions at legacy head 6ad7aebb8c7c; provenance identifies the revision
that last defined each function or trigger. Keep this snapshot immutable;
subsequent behavior changes belong in subsequent Alembic revisions.
"""

from sqlalchemy import text


def install_integration_guards(bind) -> None:
    """Install or repair the baseline guards, including already-stamped databases."""
    for statement in FUNCTIONS:
        bind.execute(text(statement))
    for name, table, statement in TRIGGERS:
        bind.execute(text(f"DROP TRIGGER IF EXISTS {name} ON {table}"))
        bind.execute(text(statement))


FUNCTIONS = (
    # 3f30b34c7e7c: integration_member_is_mutable
    """
    CREATE OR REPLACE FUNCTION integration_member_is_mutable() RETURNS trigger AS $$
                BEGIN
                    IF TG_OP <> 'INSERT' AND NOT EXISTS (
                        SELECT 1 FROM integration_batches WHERE id = OLD.batch_id AND lifecycle = 'sealing'
                    ) THEN
                        RAISE EXCEPTION 'sealed integration batch membership is immutable';
                    END IF;
                    IF TG_OP <> 'DELETE' AND NOT EXISTS (
                        SELECT 1 FROM integration_batches WHERE id = NEW.batch_id AND lifecycle = 'sealing'
                    ) THEN
                        RAISE EXCEPTION 'sealed integration batch membership is immutable';
                    END IF;
                    IF TG_OP = 'DELETE' THEN RETURN OLD;
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
    """,
    # 3f30b34c7e7c: integration_checkpoint_is_monotone
    """
    CREATE OR REPLACE FUNCTION integration_checkpoint_is_monotone() RETURNS trigger AS $$
                    BEGIN
                        IF NEW.generation < OLD.generation OR NEW.version < OLD.version THEN RAISE EXCEPTION 'checkpoint generation and version cannot decrease'; END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql
    """,
    # 3f30b34c7e7c: integration_batch_revision_is_monotone
    """
    CREATE OR REPLACE FUNCTION integration_batch_revision_is_monotone() RETURNS trigger AS $$
                    BEGIN
                        IF NEW.current_revision < OLD.current_revision THEN RAISE EXCEPTION 'integration batch revision cannot decrease'; END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql
    """,
    # 3f30b34c7e7c: integration_branch_fence_is_monotone
    """
    CREATE OR REPLACE FUNCTION integration_branch_fence_is_monotone() RETURNS trigger AS $$
                    BEGIN
                        IF NEW.fence_token < OLD.fence_token THEN RAISE EXCEPTION 'integration branch fence cannot decrease'; END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql
    """,
    # 3f30b34c7e7c: integration_lease_fence_is_monotone
    """
    CREATE OR REPLACE FUNCTION integration_lease_fence_is_monotone() RETURNS trigger AS $$
                    BEGIN
                        IF NEW.fence_token < OLD.fence_token THEN RAISE EXCEPTION 'integration lease fence cannot decrease'; END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql
    """,
    # 3f30b34c7e7c: integration_schedule_sequence_is_monotone
    """
    CREATE OR REPLACE FUNCTION integration_schedule_sequence_is_monotone() RETURNS trigger AS $$
                    BEGIN
                        IF NEW.request_sequence < OLD.request_sequence THEN RAISE EXCEPTION 'integration schedule request sequence cannot decrease'; END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql
    """,
    # 3f30b34c7e7c: integration_outbox_attempts_monotone
    """
    CREATE OR REPLACE FUNCTION integration_outbox_attempts_monotone() RETURNS trigger AS $$
                    BEGIN
                        IF NEW.attempts < OLD.attempts THEN RAISE EXCEPTION 'integration outbox attempts cannot decrease'; END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql
    """,
    # a12a5e1e4f05: integration_repair_attempts_monotone
    """
    CREATE OR REPLACE FUNCTION integration_repair_attempts_monotone() RETURNS trigger AS $$
                BEGIN
                    IF NEW.attempts < OLD.attempts AND NOT (OLD.state IN ('failed', 'expired', 'cancelled') AND NEW.state = 'active') THEN
                        RAISE EXCEPTION 'integration repair attempts cannot decrease';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
    """,
    # 3f30b34c7e7c: integration_candidate_progress_monotone
    """
    CREATE OR REPLACE FUNCTION integration_candidate_progress_monotone() RETURNS trigger AS $$
                    BEGIN
                        IF NEW.next_member_ordinal < OLD.next_member_ordinal THEN RAISE EXCEPTION 'integration candidate progress cannot decrease'; END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql
    """,
    # 3f30b34c7e7c: integration_repair_operation_stage_monotone
    """
    CREATE OR REPLACE FUNCTION integration_repair_operation_stage_monotone() RETURNS trigger AS $$
                    BEGIN
                        IF NEW.active_stage < OLD.active_stage THEN RAISE EXCEPTION 'integration repair operation stage cannot decrease'; END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql
    """,
    # e9b2f1b7c3d5: integration_prepared_identity_immutable
    """
    CREATE OR REPLACE FUNCTION integration_prepared_identity_immutable() RETURNS trigger AS $$ BEGIN IF OLD.prepared_sha IS NOT NULL AND (NEW.domain_key IS DISTINCT FROM OLD.domain_key OR NEW.receipt_id IS DISTINCT FROM OLD.receipt_id OR NEW.source_task_id IS DISTINCT FROM OLD.source_task_id OR NEW.source_head IS DISTINCT FROM OLD.source_head OR NEW.source_base IS DISTINCT FROM OLD.source_base OR NEW.repository_id IS DISTINCT FROM OLD.repository_id OR NEW.target_branch IS DISTINCT FROM OLD.target_branch OR NEW.expected_target IS DISTINCT FROM OLD.expected_target OR NEW.prepared_sha IS DISTINCT FROM OLD.prepared_sha OR NEW.fence_owner_id IS DISTINCT FROM OLD.fence_owner_id OR NEW.fence_token IS DISTINCT FROM OLD.fence_token OR NEW.recovery_ref IS DISTINCT FROM OLD.recovery_ref OR NEW.operation_key IS DISTINCT FROM OLD.operation_key OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.target_task_id IS DISTINCT FROM OLD.target_task_id OR NEW.origin_url IS DISTINCT FROM OLD.origin_url OR NEW.review_evidence::text IS DISTINCT FROM OLD.review_evidence::text OR NEW.authors::text IS DISTINCT FROM OLD.authors::text OR NEW.provenance::text IS DISTINCT FROM OLD.provenance::text OR NEW.commit_metadata::text IS DISTINCT FROM OLD.commit_metadata::text OR NEW.intent_kind IS DISTINCT FROM OLD.intent_kind OR NEW.root_batch_id IS DISTINCT FROM OLD.root_batch_id OR NEW.root_candidate_revision IS DISTINCT FROM OLD.root_candidate_revision OR NEW.project_lease_owner_id IS DISTINCT FROM OLD.project_lease_owner_id OR NEW.project_lease_fence_token IS DISTINCT FROM OLD.project_lease_fence_token OR NEW.branch_fence_owner_id IS DISTINCT FROM OLD.branch_fence_owner_id OR NEW.branch_fence_token IS DISTINCT FROM OLD.branch_fence_token OR NEW.ci_evidence_id IS DISTINCT FROM OLD.ci_evidence_id) THEN RAISE EXCEPTION 'prepared integration identity is immutable'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql
    """,
    # e9b2f1b7c3d5: task_delivery_receipt_append_only
    """
    CREATE OR REPLACE FUNCTION task_delivery_receipt_append_only()
                RETURNS trigger AS $$ BEGIN
                RAISE EXCEPTION 'task delivery receipts are append-only'; END;
                $$ LANGUAGE plpgsql
    """,
    # 3f30b34c7e7c: task_branch_origin_materialized_immutable
    """
    CREATE OR REPLACE FUNCTION task_branch_origin_materialized_immutable() RETURNS trigger AS $$
                BEGIN
                    IF OLD.materialized THEN RAISE EXCEPTION 'materialized task branch origin is immutable'; END IF;
                    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
    """,
    # a7c4d9e2106b: integration_outbox_cursor_monotone
    """
    CREATE OR REPLACE FUNCTION integration_outbox_cursor_monotone() RETURNS trigger AS $$ BEGIN IF NEW.acceptance_cursor < OLD.acceptance_cursor THEN RAISE EXCEPTION 'integration outbox acceptance cursor cannot decrease'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql
    """,
    # b91e4d7a2c10: integration_review_evidence_append_only
    """
    CREATE OR REPLACE FUNCTION integration_review_evidence_append_only() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'integration review evidence is append-only'; END; $$ LANGUAGE plpgsql
    """,
    # e4c6a8b20d31: integration_parent_audit_append_only
    """
    CREATE OR REPLACE FUNCTION integration_parent_audit_append_only() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'integration parent evidence is append-only'; END; $$ LANGUAGE plpgsql
    """,
    # d8e9f0a1b2c3: integration_batch_identity_is_immutable
    """
    CREATE OR REPLACE FUNCTION integration_batch_identity_is_immutable() RETURNS trigger AS $$ BEGIN IF TG_OP = 'UPDATE' THEN IF (OLD.lifecycle <> 'sealing' OR NEW.lifecycle <> 'sealing') AND (NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.repository_id IS DISTINCT FROM OLD.repository_id OR NEW.request_id IS DISTINCT FROM OLD.request_id OR NEW.trigger IS DISTINCT FROM OLD.trigger OR NEW.source_manifest_digest IS DISTINCT FROM OLD.source_manifest_digest OR NEW.base_sha IS DISTINCT FROM OLD.base_sha OR NEW.integration_branch IS DISTINCT FROM OLD.integration_branch OR NEW.policy_snapshot::text IS DISTINCT FROM OLD.policy_snapshot::text OR NEW.artifact_snapshot::text IS DISTINCT FROM OLD.artifact_snapshot::text OR NEW.created_at IS DISTINCT FROM OLD.created_at) THEN RAISE EXCEPTION 'sealed integration batch identity is immutable'; END IF; IF OLD.lifecycle <> 'sealing' AND NEW.lifecycle = 'sealing' THEN RAISE EXCEPTION 'integration batch cannot return to sealing'; END IF; END IF; IF NEW.lifecycle = 'empty' AND (EXISTS (SELECT 1 FROM integration_batch_members WHERE batch_id = NEW.id) OR EXISTS (SELECT 1 FROM integration_repair_operations WHERE batch_id = NEW.id) OR EXISTS (SELECT 1 FROM project_integration_leases WHERE batch_id = NEW.id)) THEN RAISE EXCEPTION 'empty integration batch cannot retain members, repair operations, or leases'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql
    """,
    # d8e9f0a1b2c3: integration_empty_batch_target_rejected
    """
    CREATE OR REPLACE FUNCTION integration_empty_batch_target_rejected() RETURNS trigger AS $$ BEGIN IF NEW.batch_id IS NOT NULL AND EXISTS (SELECT 1 FROM integration_batches WHERE id = NEW.batch_id AND lifecycle = 'empty') THEN RAISE EXCEPTION 'empty integration batch cannot have a repair operation or lease'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql
    """,
    # e1eab6dbc186: integration_candidate_publication_is_monotone
    """
    CREATE OR REPLACE FUNCTION integration_candidate_publication_is_monotone()
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
                RETURN NEW; END; $$ LANGUAGE plpgsql
    """,
    # 46f910d0dce6: integration_candidate_resolution_is_monotone
    """
    CREATE OR REPLACE FUNCTION integration_candidate_resolution_is_monotone()
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
                RETURN NEW; END; $$ LANGUAGE plpgsql
    """,
    # e1eab6dbc186: integration_candidate_mutation_is_monotone
    """
    CREATE OR REPLACE FUNCTION integration_candidate_mutation_is_monotone()
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
                RETURN NEW; END; $$ LANGUAGE plpgsql
    """,
    # e9b2f1b7c3d5: integration_root_intent_terminal_guard
    """
    CREATE OR REPLACE FUNCTION integration_root_intent_terminal_guard()
                RETURNS trigger AS $$ BEGIN
                IF OLD.intent_kind = 'root' AND OLD.state IN ('committed', 'superseded')
                THEN RAISE EXCEPTION 'terminal root promotion intent is immutable'; END IF;
                RETURN NEW; END; $$ LANGUAGE plpgsql
    """,
    # e9b2f1b7c3d5: integration_root_member_append_only
    """
    CREATE OR REPLACE FUNCTION integration_root_member_append_only() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'root intent member reservations are append-only'; END; $$ LANGUAGE plpgsql
    """,
    # e9b2f1b7c3d5: integration_root_prewrite_immutable
    """
    CREATE OR REPLACE FUNCTION integration_root_prewrite_immutable()
                RETURNS trigger AS $$ BEGIN
                IF OLD.purpose = 'root_main' AND OLD.state = 'superseded' AND
                  NEW IS DISTINCT FROM OLD
                THEN RAISE EXCEPTION 'superseded root main claim is immutable'; END IF;
                IF OLD.prewrite_at IS NOT NULL AND NEW.prewrite_at IS DISTINCT FROM OLD.prewrite_at
                THEN RAISE EXCEPTION 'root main prewrite marker is immutable'; END IF;
                RETURN NEW; END; $$ LANGUAGE plpgsql
    """,
    # f0a1b2c3d4e5: integration_attestation_publication_guard
    """
    CREATE OR REPLACE FUNCTION integration_attestation_publication_guard()
                RETURNS trigger AS $$ BEGIN
                IF TG_OP = 'DELETE' THEN
                  RAISE EXCEPTION 'attestation reservations are durable'; END IF;
                IF OLD.state = 'published' AND NEW IS DISTINCT FROM OLD THEN
                  RAISE EXCEPTION 'published attestation reservation is immutable'; END IF;
                IF OLD.prewrite_at IS NOT NULL AND
                  NEW.prewrite_at IS DISTINCT FROM OLD.prewrite_at THEN
                  RAISE EXCEPTION 'attestation prewrite marker is immutable'; END IF;
                IF OLD.prewrite_at IS NOT NULL AND
                  NEW.execution_nonce IS DISTINCT FROM OLD.execution_nonce THEN
                  RAISE EXCEPTION 'marked attestation execution nonce is immutable'; END IF;
                IF NEW.id IS DISTINCT FROM OLD.id OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.batch_id IS DISTINCT FROM OLD.batch_id OR NEW.revision IS DISTINCT FROM OLD.revision OR NEW.operation_id IS DISTINCT FROM OLD.operation_id OR NEW.head_sha IS DISTINCT FROM OLD.head_sha OR NEW.ci_evidence_id IS DISTINCT FROM OLD.ci_evidence_id OR NEW.external_id IS DISTINCT FROM OLD.external_id OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                  RAISE EXCEPTION 'attestation reservation identity is immutable'; END IF;
                RETURN NEW; END; $$ LANGUAGE plpgsql
    """,
    # 18cd4540cd0d: integration_cleanup_item_guard
    """
    CREATE OR REPLACE FUNCTION integration_cleanup_item_guard()
                RETURNS trigger AS $$ BEGIN
                IF NEW.batch_id IS DISTINCT FROM OLD.batch_id OR NEW.kind IS DISTINCT FROM OLD.kind OR NEW.identity IS DISTINCT FROM OLD.identity OR NEW.domain_key IS DISTINCT FROM OLD.domain_key OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.repository_id IS DISTINCT FROM OLD.repository_id OR NEW.repository_numeric_id IS DISTINCT FROM OLD.repository_numeric_id OR NEW.repository_full_name IS DISTINCT FROM OLD.repository_full_name OR NEW.revision IS DISTINCT FROM OLD.revision OR NEW.member_ordinal IS DISTINCT FROM OLD.member_ordinal OR NEW.receipt_id IS DISTINCT FROM OLD.receipt_id OR NEW.target_ref IS DISTINCT FROM OLD.target_ref OR NEW.target_pr_number IS DISTINCT FROM OLD.target_pr_number OR NEW.target_pr_url IS DISTINCT FROM OLD.target_pr_url OR NEW.workspace_path IS DISTINCT FROM OLD.workspace_path OR NEW.expected_sha IS DISTINCT FROM OLD.expected_sha OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                  RAISE EXCEPTION 'integration cleanup identity is immutable'; END IF;
                IF OLD.state IN ('complete', 'conflict', 'failed') AND NEW IS DISTINCT FROM OLD THEN
                  RAISE EXCEPTION 'terminal integration cleanup item is immutable'; END IF;
                RETURN NEW; END; $$ LANGUAGE plpgsql
    """,
    # a10c5e1e4f01: integration_release_result_immutable
    """
    CREATE OR REPLACE FUNCTION integration_release_result_immutable() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'integration release result is immutable'; END;
                $$ LANGUAGE plpgsql
    """,
    # a10c5e1e4f02: integration_cleanup_irreversible_guard
    """
    CREATE OR REPLACE FUNCTION integration_cleanup_irreversible_guard() RETURNS trigger AS $$
                BEGIN
                  IF OLD.irreversible_nonce IS NOT NULL AND
                     (NEW.irreversible_nonce IS DISTINCT FROM OLD.irreversible_nonce OR
                      NEW.irreversible_prewrite_at IS DISTINCT FROM OLD.irreversible_prewrite_at)
                  THEN RAISE EXCEPTION 'cleanup irreversible prewrite is immutable'; END IF;
                  IF NEW.irreversible_prewrite_at IS NOT NULL AND
                     (NEW.irreversible_nonce IS NULL OR
                      (OLD.irreversible_prewrite_at IS NULL AND
                       NEW.irreversible_nonce IS DISTINCT FROM OLD.execution_nonce))
                  THEN RAISE EXCEPTION 'cleanup irreversible prewrite is not claim-owned'; END IF;
                  RETURN NEW;
                END; $$ LANGUAGE plpgsql
    """,
    # a11a5e1e4f04: integration_control_history_immutable
    """
    CREATE OR REPLACE FUNCTION integration_control_history_immutable() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'integration control history is immutable'; END;
                $$ LANGUAGE plpgsql
    """,
)

TRIGGERS = (
    # 3f30b34c7e7c
    (
        "trg_integration_checkpoint_is_monotone",
        "task_integration_checkpoints",
        """CREATE TRIGGER trg_integration_checkpoint_is_monotone BEFORE UPDATE ON task_integration_checkpoints FOR EACH ROW EXECUTE FUNCTION integration_checkpoint_is_monotone()""",
    ),
    # 3f30b34c7e7c
    (
        "trg_integration_branch_fence_is_monotone",
        "integration_branch_owners",
        """CREATE TRIGGER trg_integration_branch_fence_is_monotone BEFORE UPDATE ON integration_branch_owners FOR EACH ROW EXECUTE FUNCTION integration_branch_fence_is_monotone()""",
    ),
    # 3f30b34c7e7c
    (
        "trg_integration_lease_fence_is_monotone",
        "project_integration_leases",
        """CREATE TRIGGER trg_integration_lease_fence_is_monotone BEFORE UPDATE ON project_integration_leases FOR EACH ROW EXECUTE FUNCTION integration_lease_fence_is_monotone()""",
    ),
    # 3f30b34c7e7c
    (
        "trg_integration_schedule_sequence_is_monotone",
        "project_integration_schedules",
        """CREATE TRIGGER trg_integration_schedule_sequence_is_monotone BEFORE UPDATE ON project_integration_schedules FOR EACH ROW EXECUTE FUNCTION integration_schedule_sequence_is_monotone()""",
    ),
    # 3f30b34c7e7c
    (
        "trg_integration_outbox_attempts_monotone",
        "integration_outbox",
        """CREATE TRIGGER trg_integration_outbox_attempts_monotone BEFORE UPDATE ON integration_outbox FOR EACH ROW EXECUTE FUNCTION integration_outbox_attempts_monotone()""",
    ),
    # 3f30b34c7e7c
    (
        "trg_integration_repair_attempts_monotone",
        "integration_repair_stages",
        """CREATE TRIGGER trg_integration_repair_attempts_monotone BEFORE UPDATE ON integration_repair_stages FOR EACH ROW EXECUTE FUNCTION integration_repair_attempts_monotone()""",
    ),
    # 3f30b34c7e7c
    (
        "trg_integration_candidate_progress_monotone",
        "integration_candidate_revisions",
        """CREATE TRIGGER trg_integration_candidate_progress_monotone BEFORE UPDATE ON integration_candidate_revisions FOR EACH ROW EXECUTE FUNCTION integration_candidate_progress_monotone()""",
    ),
    # 3f30b34c7e7c
    (
        "trg_integration_repair_operation_stage_monotone",
        "integration_repair_operations",
        """CREATE TRIGGER trg_integration_repair_operation_stage_monotone BEFORE UPDATE ON integration_repair_operations FOR EACH ROW EXECUTE FUNCTION integration_repair_operation_stage_monotone()""",
    ),
    # 3f30b34c7e7c
    (
        "trg_task_branch_origins_materialized_update",
        "task_branch_origins",
        """CREATE TRIGGER trg_task_branch_origins_materialized_update BEFORE UPDATE ON task_branch_origins FOR EACH ROW EXECUTE FUNCTION task_branch_origin_materialized_immutable()""",
    ),
    # 3f30b34c7e7c
    (
        "trg_task_branch_origins_materialized_delete",
        "task_branch_origins",
        """CREATE TRIGGER trg_task_branch_origins_materialized_delete BEFORE DELETE ON task_branch_origins FOR EACH ROW EXECUTE FUNCTION task_branch_origin_materialized_immutable()""",
    ),
    # a7c4d9e2106b
    (
        "trg_integration_outbox_cursor_monotone",
        "integration_outbox",
        """CREATE TRIGGER trg_integration_outbox_cursor_monotone BEFORE UPDATE ON integration_outbox FOR EACH ROW EXECUTE FUNCTION integration_outbox_cursor_monotone()""",
    ),
    # b91e4d7a2c10
    (
        "trg_integration_review_evidence_update",
        "integration_review_evidence",
        """CREATE TRIGGER trg_integration_review_evidence_update BEFORE UPDATE ON integration_review_evidence FOR EACH ROW EXECUTE FUNCTION integration_review_evidence_append_only()""",
    ),
    # b91e4d7a2c10
    (
        "trg_integration_review_evidence_delete",
        "integration_review_evidence",
        """CREATE TRIGGER trg_integration_review_evidence_delete BEFORE DELETE ON integration_review_evidence FOR EACH ROW EXECUTE FUNCTION integration_review_evidence_append_only()""",
    ),
    # e4c6a8b20d31
    (
        "trg_integration_check_evidence_update",
        "integration_check_evidence",
        """CREATE TRIGGER trg_integration_check_evidence_update BEFORE UPDATE ON integration_check_evidence FOR EACH ROW EXECUTE FUNCTION integration_parent_audit_append_only()""",
    ),
    # e4c6a8b20d31
    (
        "trg_integration_check_evidence_delete",
        "integration_check_evidence",
        """CREATE TRIGGER trg_integration_check_evidence_delete BEFORE DELETE ON integration_check_evidence FOR EACH ROW EXECUTE FUNCTION integration_parent_audit_append_only()""",
    ),
    # e4c6a8b20d31
    (
        "trg_integration_parent_episodes_update",
        "integration_parent_episodes",
        """CREATE TRIGGER trg_integration_parent_episodes_update BEFORE UPDATE ON integration_parent_episodes FOR EACH ROW EXECUTE FUNCTION integration_parent_audit_append_only()""",
    ),
    # e4c6a8b20d31
    (
        "trg_integration_parent_episodes_delete",
        "integration_parent_episodes",
        """CREATE TRIGGER trg_integration_parent_episodes_delete BEFORE DELETE ON integration_parent_episodes FOR EACH ROW EXECUTE FUNCTION integration_parent_audit_append_only()""",
    ),
    # e4c6a8b20d31
    (
        "trg_integration_parent_verifications_update",
        "integration_parent_verifications",
        """CREATE TRIGGER trg_integration_parent_verifications_update BEFORE UPDATE ON integration_parent_verifications FOR EACH ROW EXECUTE FUNCTION integration_parent_audit_append_only()""",
    ),
    # e4c6a8b20d31
    (
        "trg_integration_parent_verifications_delete",
        "integration_parent_verifications",
        """CREATE TRIGGER trg_integration_parent_verifications_delete BEFORE DELETE ON integration_parent_verifications FOR EACH ROW EXECUTE FUNCTION integration_parent_audit_append_only()""",
    ),
    # e4c6a8b20d31
    (
        "trg_integration_parent_operation_completions_update",
        "integration_parent_operation_completions",
        """CREATE TRIGGER trg_integration_parent_operation_completions_update BEFORE UPDATE ON integration_parent_operation_completions FOR EACH ROW EXECUTE FUNCTION integration_parent_audit_append_only()""",
    ),
    # e4c6a8b20d31
    (
        "trg_integration_parent_operation_completions_delete",
        "integration_parent_operation_completions",
        """CREATE TRIGGER trg_integration_parent_operation_completions_delete BEFORE DELETE ON integration_parent_operation_completions FOR EACH ROW EXECUTE FUNCTION integration_parent_audit_append_only()""",
    ),
    # e4c6a8b20d31
    (
        "trg_integration_parent_verification_evidence_update",
        "integration_parent_verification_evidence",
        """CREATE TRIGGER trg_integration_parent_verification_evidence_update BEFORE UPDATE ON integration_parent_verification_evidence FOR EACH ROW EXECUTE FUNCTION integration_parent_audit_append_only()""",
    ),
    # e4c6a8b20d31
    (
        "trg_integration_parent_verification_evidence_delete",
        "integration_parent_verification_evidence",
        """CREATE TRIGGER trg_integration_parent_verification_evidence_delete BEFORE DELETE ON integration_parent_verification_evidence FOR EACH ROW EXECUTE FUNCTION integration_parent_audit_append_only()""",
    ),
    # e4c6a8b20d31
    (
        "trg_integration_episode_receipt_acceptances_update",
        "integration_episode_receipt_acceptances",
        """CREATE TRIGGER trg_integration_episode_receipt_acceptances_update BEFORE UPDATE ON integration_episode_receipt_acceptances FOR EACH ROW EXECUTE FUNCTION integration_parent_audit_append_only()""",
    ),
    # e4c6a8b20d31
    (
        "trg_integration_episode_receipt_acceptances_delete",
        "integration_episode_receipt_acceptances",
        """CREATE TRIGGER trg_integration_episode_receipt_acceptances_delete BEFORE DELETE ON integration_episode_receipt_acceptances FOR EACH ROW EXECUTE FUNCTION integration_parent_audit_append_only()""",
    ),
    # d8e9f0a1b2c3
    (
        "trg_integration_members_insert",
        "integration_batch_members",
        """CREATE TRIGGER trg_integration_members_insert BEFORE INSERT ON integration_batch_members FOR EACH ROW EXECUTE FUNCTION integration_member_is_mutable()""",
    ),
    # d8e9f0a1b2c3
    (
        "trg_integration_members_update",
        "integration_batch_members",
        """CREATE TRIGGER trg_integration_members_update BEFORE UPDATE ON integration_batch_members FOR EACH ROW EXECUTE FUNCTION integration_member_is_mutable()""",
    ),
    # d8e9f0a1b2c3
    (
        "trg_integration_members_delete",
        "integration_batch_members",
        """CREATE TRIGGER trg_integration_members_delete BEFORE DELETE ON integration_batch_members FOR EACH ROW EXECUTE FUNCTION integration_member_is_mutable()""",
    ),
    # d8e9f0a1b2c3
    (
        "trg_integration_batch_revision_is_monotone",
        "integration_batches",
        """CREATE TRIGGER trg_integration_batch_revision_is_monotone BEFORE UPDATE ON integration_batches FOR EACH ROW EXECUTE FUNCTION integration_batch_revision_is_monotone()""",
    ),
    # d8e9f0a1b2c3
    (
        "trg_integration_batch_identity_immutable",
        "integration_batches",
        """CREATE TRIGGER trg_integration_batch_identity_immutable BEFORE INSERT OR UPDATE ON integration_batches FOR EACH ROW EXECUTE FUNCTION integration_batch_identity_is_immutable()""",
    ),
    # d8e9f0a1b2c3
    (
        "trg_integration_repair_operations_reject_empty_batch",
        "integration_repair_operations",
        """CREATE TRIGGER trg_integration_repair_operations_reject_empty_batch BEFORE INSERT OR UPDATE ON integration_repair_operations FOR EACH ROW EXECUTE FUNCTION integration_empty_batch_target_rejected()""",
    ),
    # d8e9f0a1b2c3
    (
        "trg_project_integration_leases_reject_empty_batch",
        "project_integration_leases",
        """CREATE TRIGGER trg_project_integration_leases_reject_empty_batch BEFORE INSERT OR UPDATE ON project_integration_leases FOR EACH ROW EXECUTE FUNCTION integration_empty_batch_target_rejected()""",
    ),
    # 69416e65ee21
    (
        "trg_candidate_publication_monotone",
        "integration_candidate_publications",
        """CREATE TRIGGER trg_candidate_publication_monotone BEFORE UPDATE ON integration_candidate_publications FOR EACH ROW EXECUTE FUNCTION integration_candidate_publication_is_monotone()""",
    ),
    # 69416e65ee21
    (
        "trg_candidate_resolution_monotone",
        "integration_candidate_resolutions",
        """CREATE TRIGGER trg_candidate_resolution_monotone BEFORE UPDATE ON integration_candidate_resolutions FOR EACH ROW EXECUTE FUNCTION integration_candidate_resolution_is_monotone()""",
    ),
    # e1eab6dbc186
    (
        "trg_candidate_mutation_monotone",
        "integration_candidate_ref_mutations",
        """CREATE TRIGGER trg_candidate_mutation_monotone BEFORE UPDATE ON integration_candidate_ref_mutations FOR EACH ROW EXECUTE FUNCTION integration_candidate_mutation_is_monotone()""",
    ),
    # d4a81f0c9e72
    (
        "trg_integration_prepared_identity_immutable",
        "integration_promotion_intents",
        """CREATE TRIGGER trg_integration_prepared_identity_immutable BEFORE UPDATE ON integration_promotion_intents FOR EACH ROW EXECUTE FUNCTION integration_prepared_identity_immutable()""",
    ),
    # e9b2f1b7c3d5
    (
        "trg_task_delivery_receipts_update",
        "task_delivery_receipts",
        """CREATE TRIGGER trg_task_delivery_receipts_update BEFORE UPDATE ON task_delivery_receipts FOR EACH ROW EXECUTE FUNCTION task_delivery_receipt_append_only()""",
    ),
    # e9b2f1b7c3d5
    (
        "trg_task_delivery_receipts_delete",
        "task_delivery_receipts",
        """CREATE TRIGGER trg_task_delivery_receipts_delete BEFORE DELETE ON task_delivery_receipts FOR EACH ROW EXECUTE FUNCTION task_delivery_receipt_append_only()""",
    ),
    # e9b2f1b7c3d5
    (
        "trg_integration_root_intent_terminal",
        "integration_promotion_intents",
        """CREATE TRIGGER trg_integration_root_intent_terminal BEFORE UPDATE ON integration_promotion_intents FOR EACH ROW EXECUTE FUNCTION integration_root_intent_terminal_guard()""",
    ),
    # e9b2f1b7c3d5
    (
        "trg_integration_root_member_update",
        "integration_root_intent_members",
        """CREATE TRIGGER trg_integration_root_member_update BEFORE UPDATE ON integration_root_intent_members FOR EACH ROW EXECUTE FUNCTION integration_root_member_append_only()""",
    ),
    # e9b2f1b7c3d5
    (
        "trg_integration_root_member_delete",
        "integration_root_intent_members",
        """CREATE TRIGGER trg_integration_root_member_delete BEFORE DELETE ON integration_root_intent_members FOR EACH ROW EXECUTE FUNCTION integration_root_member_append_only()""",
    ),
    # e9b2f1b7c3d5
    (
        "trg_integration_root_prewrite_immutable",
        "integration_candidate_ref_mutations",
        """CREATE TRIGGER trg_integration_root_prewrite_immutable BEFORE UPDATE ON integration_candidate_ref_mutations FOR EACH ROW EXECUTE FUNCTION integration_root_prewrite_immutable()""",
    ),
    # f0a1b2c3d4e5
    (
        "trg_integration_attestation_publication_guard",
        "integration_attestation_publications",
        """CREATE TRIGGER trg_integration_attestation_publication_guard BEFORE UPDATE OR DELETE ON integration_attestation_publications FOR EACH ROW EXECUTE FUNCTION integration_attestation_publication_guard()""",
    ),
    # 18cd4540cd0d
    (
        "trg_integration_cleanup_item_guard",
        "integration_cleanup_items",
        """CREATE TRIGGER trg_integration_cleanup_item_guard BEFORE UPDATE ON integration_cleanup_items FOR EACH ROW EXECUTE FUNCTION integration_cleanup_item_guard()""",
    ),
    # a10c5e1e4f01
    (
        "trg_integration_release_result_immutable",
        "integration_release_results",
        """CREATE TRIGGER trg_integration_release_result_immutable
            BEFORE UPDATE OR DELETE ON integration_release_results
            FOR EACH ROW EXECUTE FUNCTION integration_release_result_immutable()""",
    ),
    # a10c5e1e4f02
    (
        "trg_integration_cleanup_irreversible_guard",
        "integration_cleanup_items",
        """CREATE TRIGGER trg_integration_cleanup_irreversible_guard
            BEFORE UPDATE ON integration_cleanup_items FOR EACH ROW
            EXECUTE FUNCTION integration_cleanup_irreversible_guard()""",
    ),
    # a11a5e1e4f04
    (
        "trg_integration_history_waivers_immutable",
        "integration_history_waivers",
        """CREATE TRIGGER trg_integration_history_waivers_immutable BEFORE UPDATE OR DELETE ON integration_history_waivers FOR EACH ROW EXECUTE FUNCTION integration_control_history_immutable()""",
    ),
    # a11a5e1e4f04
    (
        "trg_integration_rollout_transitions_immutable",
        "integration_rollout_transitions",
        """CREATE TRIGGER trg_integration_rollout_transitions_immutable BEFORE UPDATE OR DELETE ON integration_rollout_transitions FOR EACH ROW EXECUTE FUNCTION integration_control_history_immutable()""",
    ),
    # a11a5e1e4f04
    (
        "trg_integration_history_waiver_consumptions_immutable",
        "integration_history_waiver_consumptions",
        """CREATE TRIGGER trg_integration_history_waiver_consumptions_immutable BEFORE UPDATE OR DELETE ON integration_history_waiver_consumptions FOR EACH ROW EXECUTE FUNCTION integration_control_history_immutable()""",
    ),
    # a11a5e1e4f04
    (
        "trg_integration_legacy_gate_applicability_immutable",
        "integration_legacy_gate_applicability",
        """CREATE TRIGGER trg_integration_legacy_gate_applicability_immutable BEFORE UPDATE OR DELETE ON integration_legacy_gate_applicability FOR EACH ROW EXECUTE FUNCTION integration_control_history_immutable()""",
    ),
)
